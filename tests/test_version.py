"""换版状态机：未来版、同日冲突、撤回、恢复、版本隔离查询。"""
import pytest

from timetablegrab import store, version
from timetablegrab.models import ValidationReport, VersionConflict

from feedbuilders import good_tables

SOURCE = "alpha"


def _stage(con, token, effective, tables=None, published=None):
    tables = tables or good_tables()
    manifest = {"version_token": token, "effective_date": effective,
                "shards": []}
    state = version.register(
        con, SOURCE, token, effective,
        published or f"2026-11-{10 + int(token[1]):02d}T00:00:00Z",
        manifest, ValidationReport(SOURCE, token).to_dict(), tables)
    return state


def test_future_version_stays_staged_until_due(con):
    assert _stage(con, "v1", "2026-12-25") == "staged"
    assert version.activate_due(con, SOURCE, today="2026-12-01") is None
    assert store.active_version(con, SOURCE) is None
    activated = version.activate_due(con, SOURCE, today="2026-12-25")
    assert activated == "v1"
    assert store.get_feed_version(con, SOURCE, "v1")["state"] == "active"


def test_early_activation_of_future_version_rejected(con):
    _stage(con, "v1", "2026-12-25")
    with pytest.raises(VersionConflict, match="未来版"):
        version.activate(con, SOURCE, "v1", today="2026-12-01")


def test_same_day_conflict_second_version_canceled(con):
    _stage(con, "v1", "2026-12-25", published="2026-11-10T00:00:00Z")
    with pytest.raises(VersionConflict, match="待激活版本"):
        _stage(con, "v2", "2026-12-25", published="2026-11-11T00:00:00Z")
    assert store.get_feed_version(con, SOURCE, "v2")["state"] == "canceled"
    # 不同日期的版本不受影响
    assert _stage(con, "v3", "2027-01-01") == "staged"


def test_supersede_and_history_remains_readable(con):
    _stage(con, "v1", "2026-12-01")
    version.activate(con, SOURCE, "v1", today="2026-12-01")
    _stage(con, "v2", "2027-01-01")
    version.activate(con, SOURCE, "v2", today="2027-01-01")
    assert store.active_version(con, SOURCE) == "v2"
    assert store.get_feed_version(con, SOURCE, "v1")["state"] == "superseded"
    # 旧版数据仍可按显式 token 读取
    assert version.query_table(con, SOURCE, "stops", token="v1")
    journal = store.journal_tail(con, SOURCE)
    assert any(j["phase"] == "activated" for j in journal)


def test_withdraw_staged_and_active(con):
    _stage(con, "v1", "2026-12-01")
    version.activate(con, SOURCE, "v1", today="2026-12-01")
    _stage(con, "v2", "2027-01-01")
    assert version.withdraw(con, SOURCE, "v2", reason="运营撤回") == "withdrawn"

    assert version.withdraw(con, SOURCE, "v1") == "withdrawn"
    # 无历史版本可接替时，查询返回空而非报错/拼接
    assert version.query_stops(con, SOURCE) == []


def test_withdraw_active_falls_back_to_previous(con):
    _stage(con, "v1", "2026-12-01")
    version.activate(con, SOURCE, "v1", today="2026-12-01")
    _stage(con, "v2", "2027-01-01")
    version.activate(con, SOURCE, "v2", today="2027-01-01")
    version.withdraw(con, SOURCE, "v2")
    assert store.active_version(con, SOURCE) == "v1"


def test_recover_finishes_interrupted_withdrawal(con):
    _stage(con, "v1", "2026-12-01")
    version.activate(con, SOURCE, "v1", today="2026-12-01")
    # 模拟中断：日志停在 withdrawing，状态仍是 active
    store.write_journal(con, SOURCE, "v1", "withdrawing", {"reason": "crash"})
    con.commit()
    out = version.recover(con, SOURCE)
    assert out["recovered"] and out["action"] == "finish_withdraw"
    assert store.get_feed_version(con, SOURCE, "v1")["state"] == "withdrawn"


def test_recover_is_idempotent_when_consistent(con):
    _stage(con, "v1", "2026-12-01")
    version.activate(con, SOURCE, "v1", today="2026-12-01")
    out = version.recover(con, SOURCE)
    assert out["recovered"] is False


def test_query_never_mixes_versions(con):
    t1 = good_tables()
    t2 = good_tables()
    t2["stops"][0]["name"] = "改名后的站"
    _stage(con, "v1", "2026-12-01", tables=t1)
    version.activate(con, SOURCE, "v1", today="2026-12-01")
    _stage(con, "v2", "2027-01-01", tables=t2)
    version.activate(con, SOURCE, "v2", today="2027-01-01")
    current = version.query_stops(con, SOURCE)
    old = version.query_stops(con, SOURCE, token="v1")
    assert current[0]["name"] == "改名后的站"
    assert old[0]["name"] == "人民广场"
    # on_date 只能取到当时已生效的版本
    assert store.active_version(con, SOURCE, on_date="2026-12-15") == "v1"


def test_backdated_effective_date_rejected(con):
    with pytest.raises(VersionConflict, match="已过"):
        _stage(con, "v1", "2020-01-01")


def test_conflicted_version_keeps_diff_and_alignment_evidence(con):
    """同日冲突取消的版本，其差分/对齐/原始依据仍须可读（不回滚证据）。"""
    base = good_tables(lambda i: f"OLD-{i}")
    nxt = good_tables(lambda i: f"NEW-{i}")
    _stage(con, "v1", "2026-12-25", tables=base,
           published="2026-11-10T00:00:00Z")
    # 直接构造 v2 的对齐与差分依据并预提交，模拟流水线在冲突前的留痕
    from timetablegrab import align, diff
    al = align.align_stops(base, nxt, SOURCE, "v1", "v2")
    rep = diff.diff_tables(base, nxt, SOURCE, "v1", "v2", al)
    store.save_alignment_report(con, SOURCE, "v1", "v2", al.to_dict())
    store.save_diff(con, SOURCE, "v1", "v2", rep.to_dict())
    con.commit()
    with pytest.raises(VersionConflict):
        _stage(con, "v2", "2026-12-25", tables=nxt,
               published="2026-11-11T00:00:00Z")
    assert store.get_alignment_report(con, SOURCE, "v1", "v2") is not None
    assert store.get_diff(con, SOURCE, "v1", "v2")["summary"]["id_drift"] >= 3
