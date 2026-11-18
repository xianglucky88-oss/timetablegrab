"""薄编排层。

- run()：遗留通用记录流水线（loader → parse→feed_pull→align→version → keyer → store），
  语义与旧版完全一致，保证旧合法输入与旧测试不受影响。
- ingest_feed()：时刻表整包流水线，只负责按顺序调用单一职责阶段，
  不在编排层加入业务判断；阻断/冲突由各阶段的确定异常向上传播。
"""
from __future__ import annotations
import importlib
from pathlib import Path

from . import align as _align
from . import diff as _diff
from . import feed_pull as _feed_pull
from . import loader as _loader
from . import parse as _parse
from . import store as _store
from . import validate as _validate
from . import version as _version
from .keyer import key_of
from .models import Record, ValidationError

CHAIN_NAMES = ['parse', 'feed_pull', 'align', 'version']


def run(con=None):
    own = con is None
    con = con or _store.connect()
    try:
        chain = []
        for n in CHAIN_NAMES:
            mod = importlib.import_module("." + n, __package__)
            chain.append(mod.process)
        fed = 0
        for raw in _loader.load():
            rec = raw
            for fn in chain:
                rec = fn(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            fed += 1
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()


def ingest_feed(source: str, transport, con=None, today: str | None = None) -> dict:
    """时刻表整包流水线：下载 → 解析 → 校验 → 对齐 → 差分 → 登记/换版。

    返回各阶段摘要。下载不完整抛 PullError；校验阻断时登记 rejected 后重抛，
    残缺版本永远不会进入 staged/active。
    """
    own = con is None
    con = con or _store.connect()
    try:
        pulled = _feed_pull.pull(source, transport)
        manifest = pulled["manifest"]
        token = manifest.version_token

        parse_results = _parse.parse_bundle(pulled["attempt_dir"], manifest)
        rows = [r for pr in parse_results for r in pr.rows]
        parse_blocked = [pr for pr in parse_results if pr.blocked]

        report, clean = _validate.validate(rows, source, token)
        for pr in parse_blocked:  # 解析阶段的 BLOCKER 并入同一份校验依据
            report.findings.extend(pr.findings)

        # 原始文件与来源版本归档（保留依据，含哈希）
        for spec in manifest.shards:
            content = (Path(pulled["attempt_dir"]) / spec.name).read_bytes()
            _store.archive_raw(con, source, token, spec.name, spec.sha256, content)

        if report.blocked:
            _version.register_rejected(
                con, source, token, manifest.effective_date,
                manifest.published_at, manifest.to_dict(), report.to_dict())
            raise ValidationError(report)

        # 差分基线取最近的可用版本（含 staged），保证未来版之间也能对齐与留痕
        base_token = _store.latest_version_token(con, source)
        base_tables = _load_all_tables(con, source, base_token) if base_token else {}
        if base_token:
            alignment = _align.align_stops(base_tables, clean, source,
                                           base_token, token)
            _store.save_alignment_report(
                con, source, base_token, token, alignment.to_dict())
            diff_report = _diff.diff_tables(
                base_tables, clean, source, base_token, token, alignment)
        else:
            alignment = None
            diff_report = _diff.diff_tables(
                base_tables, clean, source, "", token, None)
        _store.save_diff(con, source, base_token or "", token,
                         diff_report.to_dict())
        con.commit()  # 依据先持久化：后续即便同日冲突，差分/对齐/原始件仍可读

        state = _version.register(
            con, source, token, manifest.effective_date,
            manifest.published_at, manifest.to_dict(),
            report.to_dict(), clean)
        activated = None
        if state == "staged":
            activated = _version.activate_due(con, source, today=today)
        return {
            "feed_source": source,
            "version_token": token,
            "state": state,
            "activated_token": activated,
            "downloaded": pulled["downloaded"],
            "from_cache": pulled["from_cache"],
            "resumed": pulled["resumed"],
            "validation": report.to_dict(),
            "diff": diff_report.to_dict(),
            "alignment": alignment.to_dict() if alignment else None,
        }
    finally:
        if own:
            con.close()


def _load_all_tables(con, source, token) -> dict:
    return {name: (_store.version_rows(con, source, token, name) or [])
            for name in ("stops", "routes", "trips", "stop_times", "calendar")}
