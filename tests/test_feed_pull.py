"""下载阶段：缺分片、损坏、陈旧响应、条件缓存、中断恢复、资源限制。"""
import json

import pytest

from timetablegrab import config, feed_pull
from timetablegrab.models import PullError

from feedbuilders import (FlakyTransport, ManifestTokenTransport,
                          corrupt_one_shard, good_tables, json_bundle,
                          write_source)


def _source(tmp_path, source="alpha", token="v1", effective="2026-12-01"):
    tables = good_tables()
    shards = json_bundle(tables)
    sdir = write_source(tmp_path / "src", source, token, effective, shards)
    return sdir, shards


def test_full_pull_completes_and_is_idempotent(sandbox):
    sdir, shards = _source(sandbox)
    t = feed_pull.FileTransport({"alpha": sdir})
    r1 = feed_pull.pull("alpha", t)
    assert r1["complete"] is True and r1["version_token"] == "v1"
    again = feed_pull.pull("alpha", t)  # 已完整：直接复用，零网络分片
    assert again["complete"] and again["resumed"] and again["downloaded"] == []
    assert feed_pull.completed_bundle("alpha", "v1") is not None


def test_corrupt_shard_is_quarantined_and_blocks(sandbox):
    sdir, shards = _source(sandbox)
    corrupt_one_shard(sdir, "stops.json", truncate=True)
    t = feed_pull.FileTransport({"alpha": sdir})
    with pytest.raises(PullError, match="损坏或被截断"):
        feed_pull.pull("alpha", t)
    # 隔离区有坏文件与原因说明；包目录没有 COMPLETE 标记
    quarantined = list((sandbox / "quarantine_dir").rglob("*stops*"))
    assert quarantined
    assert feed_pull.completed_bundle("alpha", "v1") is None
    # 缓存未被污染：缓存目录中没有该坏内容
    assert not list((sandbox / "cache_dir").rglob("*"))


def test_missing_shard_half_package_cannot_activate(sandbox):
    sdir, shards = _source(sandbox)
    (sdir / "trips.json").unlink()  # 模拟“半个包”
    t = feed_pull.FileTransport({"alpha": sdir})
    with pytest.raises((PullError, FileNotFoundError)):
        feed_pull.pull("alpha", t)
    assert feed_pull.completed_bundle("alpha", "v1") is None


def test_cache_reuses_only_verified_content(sandbox):
    sdir, _ = _source(sandbox)
    t = feed_pull.FileTransport({"alpha": sdir})
    feed_pull.pull("alpha", t)
    # 删掉尝试目录，模拟全新下载：完整分片应全部命中条件缓存
    import shutil
    shutil.rmtree(sandbox / "data_dir")
    r = feed_pull.pull("alpha", t)
    assert r["complete"]
    assert set(r["from_cache"]) == {"stops.json", "routes.json", "trips.json",
                                    "stop_times.json", "calendar.json"}


def test_resume_after_interruption(sandbox):
    sdir, _ = _source(sandbox)
    flaky = FlakyTransport({"alpha": sdir}, fail_n=1, fail_shards={"stops.json"})
    with pytest.raises(PullError):
        feed_pull.pull("alpha", flaky)
    # 中断后重入：journal 记录了失败，后续分片成功，整体可恢复完成
    flaky.attempts = 99  # 此后不再失败
    r = feed_pull.pull("alpha", flaky)
    assert r["complete"] and r["resumed"]


def test_stale_shard_response_rejected(sandbox):
    sdir, _ = _source(sandbox)
    t = FlakyTransport({"alpha": sdir}, stale_token="v0-old")
    with pytest.raises(PullError, match="陈旧响应"):
        feed_pull.pull("alpha", t)


def test_stale_manifest_response_rejected(sandbox):
    sdir, _ = _source(sandbox)
    t = ManifestTokenTransport({"alpha": sdir})
    with pytest.raises(PullError, match="陈旧|错版"):
        feed_pull.pull("alpha", t)


def test_resource_limit_shard_size(sandbox, monkeypatch):
    sdir, shards = _source(sandbox)
    monkeypatch.setattr(config, "MAX_SHARD_BYTES", 10)
    t = feed_pull.FileTransport({"alpha": sdir})
    with pytest.raises(PullError, match="资源限制"):
        feed_pull.pull("alpha", t)


def test_each_attempt_uses_fresh_directory(sandbox):
    sdir, _ = _source(sandbox)
    t = feed_pull.FileTransport({"alpha": sdir})
    with pytest.raises(PullError):
        feed_pull.pull("alpha", FlakyTransport(
            {"alpha": sdir}, fail_n=99, fail_shards={"stops.json"}))
    attempts = list((sandbox / "data_dir" / "alpha" / "v1").glob("attempt-*"))
    assert len(attempts) == 1  # 同版本复用同一尝试目录，不产生散落目录


def test_retry_budget_blocks_permanently_broken_shard(sandbox, monkeypatch):
    monkeypatch.setattr(config, "MAX_DOWNLOAD_ATTEMPTS", 2)
    sdir, _ = _source(sandbox)
    # 始终返回截断分片：超过重试预算后残缺版本确定阻断
    t = FlakyTransport({"alpha": sdir}, fail_n=10**9,
                       fail_shards={"stops.json"})
    # 预算 2 = 初次尝试 + 2 次重试，均失败；第 4 次进入被上限拦截
    for _ in range(config.MAX_DOWNLOAD_ATTEMPTS + 1):
        with pytest.raises(PullError):
            feed_pull.pull("alpha", t)
    with pytest.raises(PullError, match="重试已达上限"):
        feed_pull.pull("alpha", t)
    assert feed_pull.completed_bundle("alpha", "v1") is None
    assert feed_pull.completed_bundle("alpha", "v1") is None
