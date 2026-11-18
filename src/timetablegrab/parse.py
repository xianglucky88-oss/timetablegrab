"""单一职责：把原始分片解析为带行定位的行记录，不做业务规范化与校验。

界线：
- 解析只回答“字节能否成为结构化行”，并保留 source_file + line 依据；
  外键、重复键、顺序等业务规则属于 validate 阶段。
- JSON 整体失败、JSONL 单行失败、非对象行、行数超限均为 BLOCKER：
  局部解析失败的分片 ok=False，缓存与换版流程不得把它当作成功。
"""
from __future__ import annotations
import json
from pathlib import Path

from . import config
from .models import Finding, Manifest, ParseResult, Row


def process(record):
    """遗留通用记录流水线入口（保持向后兼容，勿在新流程中使用）。"""
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    rec["value"] = int(rec.get("value", 0))
    return rec


def parse_shard(path: str | Path, table: str, kind: str,
                max_rows: int = config.MAX_ROWS_PER_SHARD) -> ParseResult:
    p = Path(path)
    result = ParseResult(shard=p.name)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        result.ok = False
        result.findings.append(Finding("BLOCKER", "UNREADABLE", str(e),
                                       table=table, file=p.name))
        return result

    if kind == "jsonl":
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            if len(result.rows) >= max_rows:
                result.ok = False
                result.findings.append(Finding(
                    "BLOCKER", "RESOURCE_LIMIT",
                    f"分片行数超过上限 {max_rows}", table=table, file=p.name, line=lineno))
                return result
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                result.ok = False
                result.findings.append(Finding(
                    "BLOCKER", "PARSE_ERROR", f"JSONL 行无法解析: {e.msg}",
                    table=table, file=p.name, line=lineno))
                continue
            if not isinstance(obj, dict):
                result.ok = False
                result.findings.append(Finding(
                    "BLOCKER", "NOT_AN_OBJECT", "行内容不是 JSON 对象",
                    table=table, file=p.name, line=lineno))
                continue
            result.rows.append(Row(table, obj, p.name, lineno))
        return result

    # kind == "json"：整体解析，失败时尽力给出错误所在行
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        result.ok = False
        result.findings.append(Finding(
            "BLOCKER", "PARSE_ERROR", f"JSON 文档无法解析: {e.msg}",
            table=table, file=p.name, line=getattr(e, "lineno", None)))
        return result

    records = obj if isinstance(obj, list) else obj.get("records", []) if isinstance(obj, dict) else None
    if records is None or not isinstance(records, list):
        result.ok = False
        result.findings.append(Finding(
            "BLOCKER", "NOT_AN_OBJECT", "JSON 顶层必须是对象数组或含 records 的对象",
            table=table, file=p.name))
        return result
    if len(records) > max_rows:
        result.ok = False
        result.findings.append(Finding(
            "BLOCKER", "RESOURCE_LIMIT",
            f"分片行数超过上限 {max_rows}", table=table, file=p.name))
        return result
    for index, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            result.ok = False
            result.findings.append(Finding(
                "BLOCKER", "NOT_AN_OBJECT", "数组元素不是 JSON 对象",
                table=table, file=p.name, line=index))
            continue
        result.rows.append(Row(table, rec, p.name, index))
    return result


def parse_bundle(attempt_dir: str | Path, manifest: Manifest) -> list[ParseResult]:
    """解析整包：每个分片一个 ParseResult，任一失败由调用方按 BLOCKER 阻断。"""
    base = Path(attempt_dir)
    out: list[ParseResult] = []
    for spec in manifest.shards:
        table = manifest.target or Path(spec.name).stem
        out.append(parse_shard(base / spec.name, table, manifest.kind))
    return out
