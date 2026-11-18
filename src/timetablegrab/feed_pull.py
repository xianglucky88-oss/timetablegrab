"""单一职责：时刻表整包下载（分片/清单），完整性门禁与可恢复日志。

设计要点（对应需求）：
- 每次下载尝试使用独立新空目录；原始文件原样保留，坏分片移入隔离区。
- 条件缓存按内容哈希复用，且只复用“大小+哈希”均匹配的完整内容；
  缺分片、损坏、超限、局部失败一律不写缓存、不算成功。
- 下载日志 journal.json 持久化每个分片状态，中断后重入可恢复。
- 服务端版本令牌与清单不一致（旧响应晚到）判为陈旧响应，拒绝采用。
"""
from __future__ import annotations
import json
import shutil
from pathlib import Path
from typing import Protocol

from . import config
from .hashutil import sha256_bytes, sha256_file
from .models import Manifest, PullError

JOURNAL = "journal.json"
COMPLETE = "COMPLETE"


def process(record):
    """遗留通用记录流水线入口（保持向后兼容）。"""
    rec = dict(record)
    rec.setdefault("_stage_feed_pull", "ok")
    return rec


class Transporter(Protocol):
    """传输抽象：离线 FileTransport / 在线 HttpTransport / 测试假源同构。"""

    def get_manifest(self, source: str) -> tuple[bytes, str]:
        """返回 (清单字节, 服务端当前版本令牌)。"""

    def get_shard(self, source: str, name: str, token: str) -> tuple[bytes, str]:
        """返回 (分片字节, 该响应实际所属版本令牌)。"""


class FileTransport:
    """离线文件源：每个来源对应一个目录，内含 manifest.json 与分片。"""

    def __init__(self, roots: dict[str, str | Path]):
        self.roots = {k: Path(v) for k, v in roots.items()}

    def get_manifest(self, source):
        root = self.roots[source]
        body = (root / "manifest.json").read_bytes()
        token = json.loads(body.decode("utf-8"))["version_token"]
        return body, token

    def get_shard(self, source, name, token):
        root = self.roots[source]
        body = (root / name).read_bytes()
        # 文件源把响应令牌记录在 <name>.token（可选），默认与清单一致
        tok_file = root / (name + ".token")
        return body, tok_file.read_text().strip() if tok_file.exists() else token


class HttpTransport:
    """在线源：httpx 可选依赖，缺失时给出确定性错误而非静默降级。"""

    def __init__(self, base_urls: dict[str, str], timeout: float | None = None):
        self.base_urls = dict(base_urls)
        self.timeout = timeout or config.DOWNLOAD_TIMEOUT_SECONDS

    def _client(self):
        try:
            import httpx
        except ImportError as e:  # pragma: no cover - 环境无 httpx 时的确定行为
            raise PullError("httpx 未安装，无法执行在线下载") from e
        return httpx.Client(timeout=self.timeout)

    def get_manifest(self, source):
        with self._client() as c:
            r = c.get(self.base_urls[source] + "/manifest.json")
            r.raise_for_status()
            token = r.headers.get("X-Feed-Token",
                                  json.loads(r.content.decode("utf-8"))["version_token"])
            return r.content, token

    def get_shard(self, source, name, token):
        with self._client() as c:
            r = c.get(self.base_urls[source] + "/" + name,
                      headers={"X-Feed-Token": token})
            r.raise_for_status()
            return r.content, r.headers.get("X-Feed-Token", token)


def _feed_dir(source: str, token: str) -> Path:
    return config.DATA_DIR / source / token


def _cache_path(source: str, digest: str) -> Path:
    return config.CACHE_DIR / source / digest


def _quarantine(source: str, digest: str, name: str, reason: str) -> Path:
    qdir = config.QUARANTINE_DIR / source
    qdir.mkdir(parents=True, exist_ok=True)
    qpath = qdir / f"{digest[:12]}__{Path(name).name}"
    meta = qpath.with_suffix(qpath.suffix + ".reason")
    meta.write_text(reason, encoding="utf-8")
    return qpath


def _read_journal(attempt: Path) -> dict:
    jp = attempt / JOURNAL
    if jp.exists():
        return json.loads(jp.read_text(encoding="utf-8"))
    return {"retries": 0, "shards": {}}


def _write_journal(attempt: Path, journal: dict) -> None:
    jp = attempt / JOURNAL
    tmp = jp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(jp)  # 原子替换，中断不会留下半份日志


def _existing_attempt(feed_dir: Path, token: str) -> Path | None:
    if not feed_dir.exists():
        return None
    for d in sorted(feed_dir.glob("attempt-*")):
        if (d / COMPLETE).exists():
            return d
        j = _read_journal(d)
        if j.get("token") == token:
            return d
    return None


def pull(source: str, transport: Transporter) -> dict:
    """执行一次（可恢复的）整包下载，返回下载结果字典。

    成功前提：清单可解析、所有分片齐全且大小/哈希与清单一致、未超资源上限、
    无陈旧响应。任一不满足即抛 :class:`PullError`，缓存不会被污染。
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    config.QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    raw_manifest, server_token = transport.get_manifest(source)
    try:
        manifest = Manifest.from_dict(json.loads(raw_manifest.decode("utf-8")))
    except (ValueError, KeyError, TypeError) as e:
        raise PullError(f"清单无法解析: {e}") from e

    if manifest.feed_source != source:
        raise PullError(f"清单来源不匹配: 期望 {source}，实际 {manifest.feed_source}")
    if not manifest.shards:
        raise PullError("清单缺少分片声明，整包不完整")
    if server_token != manifest.version_token:
        # 旧响应晚到：服务端当前令牌与清单内容不一致
        raise PullError(
            f"陈旧/错版清单响应: 响应令牌 {server_token} != 清单令牌 {manifest.version_token}")

    feed_dir = _feed_dir(source, manifest.version_token)
    existing = _existing_attempt(feed_dir, manifest.version_token)
    if existing is not None and (existing / COMPLETE).exists():
        return _success_result(manifest, existing, cached=[], downloaded=[],
                               resumed=True)

    if existing is None:
        n = len(list(feed_dir.glob("attempt-*"))) if feed_dir.exists() else 0
        attempt = feed_dir / f"attempt-{n + 1}"
        attempt.mkdir(parents=True, exist_ok=False)  # 独立新空目录
        journal = {"token": manifest.version_token, "retries": 0, "shards": {}}
        resumed = False
    else:
        attempt = existing  # 复用未完成尝试目录：中断恢复
        journal = _read_journal(attempt)
        resumed = True
        # journal.retries 记录已发生的失败尝试次数；达到预算即阻断，不再发起新尝试
        if journal.get("retries", 0) >= config.MAX_DOWNLOAD_ATTEMPTS:
            raise PullError(
                f"下载重试已达上限 {config.MAX_DOWNLOAD_ATTEMPTS}，残缺版本不予激活")
        journal["retries"] = journal.get("retries", 0) + 1
        _write_journal(attempt, journal)

    (attempt / "manifest.json").write_bytes(raw_manifest)
    total_bytes = 0
    cached, downloaded = [], []

    for spec in manifest.shards:
        total_bytes += spec.size
        if spec.size > config.MAX_SHARD_BYTES or total_bytes > config.MAX_BUNDLE_BYTES:
            raise PullError(f"超出资源限制: 分片 {spec.name} 声明 {spec.size} 字节")

        done = journal["shards"].get(spec.name)
        dest = attempt / spec.name
        if done and dest.exists():
            # 恢复路径：对“已完成”分片重新验真，防止磁盘上的残缺内容被复用
            if dest.stat().st_size == spec.size and sha256_file(dest) == spec.sha256:
                cached.append(spec.name)
                continue
            dest.unlink(missing_ok=True)

        cache = _cache_path(source, spec.sha256)
        if cache.exists() and cache.stat().st_size == spec.size \
                and sha256_file(cache) == spec.sha256:
            shutil.copyfile(cache, dest)  # 条件缓存：仅复用完整内容
            journal["shards"][spec.name] = {"status": "done", "from": "cache"}
            _write_journal(attempt, journal)
            cached.append(spec.name)
            continue

        body, resp_token = transport.get_shard(source, spec.name, manifest.version_token)
        if resp_token != manifest.version_token:
            raise PullError(
                f"分片 {spec.name} 收到陈旧响应: {resp_token} != {manifest.version_token}")
        digest = sha256_bytes(body)
        if len(body) != spec.size or digest != spec.sha256:
            _quarantine(source, digest, spec.name,
                        f"size/hash mismatch: got {len(body)} bytes sha256={digest}")
            journal["shards"][spec.name] = {"status": "corrupt", "sha256": digest}
            _write_journal(attempt, journal)
            raise PullError(
                f"分片 {spec.name} 损坏或被截断（期望 {spec.size}/{spec.sha256[:12]}，"
                f"实际 {len(body)}/{digest[:12]}），已隔离，本次下载不完整")

        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(dest)  # 原子落盘，中断不会出现“看似完整”的分片
        cache.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dest, cache)  # 仅验真通过的完整分片进入缓存
        journal["shards"][spec.name] = {"status": "done", "from": "network",
                                        "sha256": digest}
        _write_journal(attempt, journal)
        downloaded.append(spec.name)

    (attempt / COMPLETE).write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False), encoding="utf-8")
    return _success_result(manifest, attempt, cached, downloaded, resumed=resumed)


def _success_result(manifest: Manifest, attempt: Path,
                    cached: list, downloaded: list, resumed: bool) -> dict:
    return {
        "feed_source": manifest.feed_source,
        "version_token": manifest.version_token,
        "effective_date": manifest.effective_date,
        "manifest": manifest,
        "attempt_dir": attempt,
        "complete": True,
        "resumed": resumed,
        "from_cache": cached,
        "downloaded": downloaded,
    }


def completed_bundle(source: str, token: str) -> Path | None:
    """返回已完整下载的包目录；残缺/中断的包返回 None（不得当作成功）。"""
    attempt = _existing_attempt(_feed_dir(source, token), token)
    if attempt is not None and (attempt / COMPLETE).exists():
        return attempt
    return None
