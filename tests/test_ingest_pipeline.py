"""端到端流水线：残缺包不激活、ID 漂移换版、历史可读、原始依据归档。"""
import hashlib
import json

import pytest

from timetablegrab import feed_pull, pipeline, store, version
from timetablegrab.models import ValidationError

from feedbuilders import corrupt_one_shard, good_tables, json_bundle, write_source


def _build(tmp_path, token, effective, id_fn=None, source="alpha"):
    tables = good_tables(id_fn or (lambda i: f"S{i}"))
    sdir = write_source(tmp_path / "src", source, token, effective,
                        json_bundle(tables))
    return sdir, tables


def test_happy_path_ingest_activates_and_archives(sandbox, con):
    sdir, _ = _build(sandbox, "v1", "2026-11-18")
    out = pipeline.ingest_feed("alpha", feed_pull.FileTransport({"alpha": sdir}),
                               con=con, today="2026-11-18")
    # 登记为 staged 后由 activate_due 于生效日切换为 active
    assert out["state"] == "staged" and out["activated_token"] == "v1"
    assert store.get_feed_version(con, "alpha", "v1")["state"] == "active"
    assert store.active_version(con, "alpha") == "v1"
    # 原始分片与哈希已归档，可离线复核
    archived = store.raw_archive(con, "alpha", "v1")
    assert {name for name, _, _ in archived} == {
        "stops.json", "routes.json", "trips.json", "stop_times.json",
        "calendar.json"}


def test_corrupt_bundle_never_activates(sandbox, con):
    sdir, _ = _build(sandbox, "v1", "2026-11-18")
    corrupt_one_shard(sdir, "trips.json", truncate=True)
    with pytest.raises(Exception):  # PullError：下载门禁先于校验
        pipeline.ingest_feed("alpha", feed_pull.FileTransport({"alpha": sdir}),
                             con=con, today="2026-11-18")
    assert store.active_version(con, "alpha") is None


def test_blocking_validation_marks_version_rejected(sandbox, con):
    sdir, tables = _build(sandbox, "v1", "2026-11-18")
    tables["routes"][0]["stop_ids"].append("GHOST")  # 外键悬空
    body = json.dumps(tables["routes"], ensure_ascii=False).encode("utf-8")
    (sdir / "routes.json").write_bytes(body)
    manifest = json.loads((sdir / "manifest.json").read_text("utf-8"))
    for spec in manifest["shards"]:
        if spec["name"] == "routes.json":
            spec["size"], spec["sha256"] = len(body), hashlib.sha256(body).hexdigest()
    (sdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                        encoding="utf-8")
    with pytest.raises(ValidationError):
        pipeline.ingest_feed("alpha", feed_pull.FileTransport({"alpha": sdir}),
                             con=con, today="2026-11-18")
    meta = store.get_feed_version(con, "alpha", "v1")
    assert meta["state"] == "rejected"
    assert store.active_version(con, "alpha") is None
    # 校验依据保留可读
    rep = store.get_validation_report(con, "alpha", "v1")
    assert any(f["code"] == "FOREIGN_KEY" for f in rep["findings"])


def test_id_drift_version_supersedes_without_false_rewrite(sandbox, con):
    s1, _ = _build(sandbox, "v1", "2026-11-18", id_fn=lambda i: f"OLD-{i}")
    pipeline.ingest_feed("alpha", feed_pull.FileTransport({"alpha": s1}),
                         con=con, today="2026-11-18")
    s2, _ = _build(sandbox, "v2", "2026-12-01", id_fn=lambda i: f"NEW-{i}")
    out = pipeline.ingest_feed("alpha", feed_pull.FileTransport({"alpha": s2}),
                               con=con, today="2026-11-18")
    assert out["state"] == "staged"  # 未来版到日才生效
    assert out["diff"]["full_rewrite"] is False
    assert len(out["diff"]["id_drift"]) >= 3
    version.activate_due(con, "alpha", today="2026-12-01")
    assert store.active_version(con, "alpha") == "v2"
    # 旧版与历史差分保持可读
    assert version.query_table(con, "alpha", "stops", token="v1")
    assert store.get_diff(con, "alpha", "v1", "v2")["summary"]["id_drift"] >= 3


def test_same_day_conflict_cancels_but_keeps_evidence(sandbox, con):
    s1, _ = _build(sandbox, "v1", "2026-12-25", id_fn=lambda i: f"OLD-{i}")
    pipeline.ingest_feed("alpha", feed_pull.FileTransport({"alpha": s1}),
                         con=con, today="2026-11-18")
    s2, _ = _build(sandbox, "v2", "2026-12-25", id_fn=lambda i: f"NEW-{i}")
    with pytest.raises(version.VersionConflict):
        pipeline.ingest_feed("alpha", feed_pull.FileTransport({"alpha": s2}),
                             con=con, today="2026-11-18")
    assert store.get_feed_version(con, "alpha", "v2")["state"] == "canceled"
    # 即便被取消，对齐/差分/原始件依据都已持久化
    assert store.get_alignment_report(con, "alpha", "v1", "v2") is not None
    assert store.get_diff(con, "alpha", "v1", "v2") is not None
    assert len(store.raw_archive(con, "alpha", "v2")) == 5
    # 在位版本 v1 不受影响
    assert store.get_feed_version(con, "alpha", "v1")["state"] == "staged"
