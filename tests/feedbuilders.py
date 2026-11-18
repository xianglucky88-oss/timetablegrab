"""测试辅助：在临时目录构建完整/残缺的时刻表来源与假传输。"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

from timetablegrab.feed_pull import FileTransport


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def good_tables(stop_id_fn=lambda i: f"S{i}", prefix=""):
    """构造一套结构完整、可通过校验的五表数据。"""
    stops = [
        {"id": stop_id_fn(1), "name": f"{prefix}人民广场", "lat": 31.2304,
         "lon": 121.4737, "zone_id": "Z1",
         "valid_from": "2026-01-01", "valid_to": "2027-12-31"},
        {"id": stop_id_fn(2), "name": f"{prefix}南京路", "lat": 31.2350,
         "lon": 121.4800, "zone_id": "Z1",
         "valid_from": "2026-01-01", "valid_to": "2027-12-31"},
        {"id": stop_id_fn(3), "name": f"{prefix}陆家嘴", "lat": 31.2400,
         "lon": 121.4900, "zone_id": "Z2",
         "valid_from": "2026-01-01", "valid_to": "2027-12-31"},
    ]
    ids = [s["id"] for s in stops]
    routes = [{"id": "R1", "name": f"{prefix}二号线", "direction": "east",
               "stop_ids": ids}]
    calendar = [{"id": "CAL1", "start_date": "2026-01-01",
                 "end_date": "2026-12-31", "weekdays": list(range(7))}]
    trips = [{"id": "T1", "route_id": "R1", "service_id": "CAL1",
              "service_date": "2026-12-01"}]
    stop_times = [
        {"trip_id": "T1", "stop_id": ids[0], "stop_sequence": 1,
         "arrival": "08:00:00", "departure": "08:00:30"},
        {"trip_id": "T1", "stop_id": ids[1], "stop_sequence": 2,
         "arrival": "08:10:00", "departure": "08:10:30"},
        {"trip_id": "T1", "stop_id": ids[2], "stop_sequence": 3,
         "arrival": "08:20:00", "departure": "08:20:30"},
    ]
    return {"stops": stops, "routes": routes, "trips": trips,
            "stop_times": stop_times, "calendar": calendar}


def write_source(root: Path, source: str, token: str, effective: str,
                 shards: dict[str, bytes], kind: str = "json",
                 published_at: str = "2026-11-10T00:00:00Z",
                 target: str = "") -> Path:
    """在 root/source 下写 manifest + 分片（每分片可声明损坏/截断）。"""
    sdir = root / source
    sdir.mkdir(parents=True, exist_ok=True)
    specs = []
    for name, body in shards.items():
        (sdir / name).write_bytes(body)
        specs.append({"name": name, "size": len(body),
                      "sha256": sha256_bytes(body)})
    manifest = {"feed_source": source, "version_token": token,
                "effective_date": effective, "published_at": published_at,
                "kind": kind, "target": target, "shards": specs}
    (sdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return sdir


def jsonl_bundle(tables: dict) -> dict[str, bytes]:
    return {f"{t}.jsonl": "".join(
        json.dumps(r, ensure_ascii=False) + "\n" for r in rows
    ).encode("utf-8") for t, rows in tables.items()}


def json_bundle(tables: dict) -> dict[str, bytes]:
    return {f"{t}.json": json.dumps(rows, ensure_ascii=False).encode("utf-8")
            for t, rows in tables.items()}


def corrupt_one_shard(sdir: Path, name: str, *, truncate: bool = False):
    """重写分片并让 manifest 保留旧哈希（模拟半个包/损坏分片）。"""
    manifest = json.loads((sdir / "manifest.json").read_text("utf-8"))
    body = (sdir / name).read_bytes()
    bad = body[: max(1, len(body) // 2)] if truncate else body + b"\x00garbage"
    (sdir / name).write_bytes(bad)
    # manifest 的 size/sha256 保持为原始值 -> 校验必然失败
    return manifest


class FlakyTransport(FileTransport):
    """模拟：前 fail_n 次分片调用失败/截断，随后恢复；可注入陈旧令牌。"""

    def __init__(self, roots, fail_n=1, stale_token=None, fail_shards=None):
        super().__init__(roots)
        self.attempts = 0
        self.fail_n = fail_n
        self.stale_token = stale_token
        self.fail_shards = set(fail_shards or [])
        self.calls: list[tuple] = []

    def get_shard(self, source, name, token):
        self.calls.append((name, token))
        body, tok = super().get_shard(source, name, token)
        if self.stale_token is not None:
            return body, self.stale_token
        if name in self.fail_shards and self.attempts < self.fail_n:
            self.attempts += 1
            return body[: max(1, len(body) // 2)], token
        return body, tok


class ManifestTokenTransport(FileTransport):
    """清单令牌与服务端当前令牌不一致（旧清单晚到）。"""

    def get_manifest(self, source):
        body, token = super().get_manifest(source)
        return body, token + "-NEWER"
