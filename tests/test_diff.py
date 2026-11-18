"""换版差分：ID 漂移不算删除新增、整表重写误报保护、字段级变更。"""
from timetablegrab import align, diff

from feedbuilders import good_tables


def test_id_drift_is_change_not_delete_plus_add():
    old_t = good_tables(lambda i: f"OLD-{i}")
    new_t = good_tables(lambda i: f"NEW-{i}")  # 全部站点换编号，业务实体不变
    al = align.align_stops(old_t, new_t, "alpha", "v1", "v2")
    rep = diff.diff_tables(old_t, new_t, "alpha", "v1", "v2", al)
    assert rep.summary["id_drift"] >= 3
    stop_removals = [d for d in rep.removed if d["table"] == "stops"]
    stop_adds = [d for d in rep.added if d["table"] == "stops"]
    assert stop_removals == [] and stop_adds == []
    assert rep.full_rewrite is False


def test_full_rewrite_not_falsely_flagged_with_alignment():
    old_t = good_tables()
    new_t = good_tables()
    new_t["stops"][0]["name"] = "人民广场枢纽站"  # 有坐标同源，可对齐
    al = align.align_stops(old_t, new_t, "alpha", "v1", "v2")
    rep = diff.diff_tables(old_t, new_t, "alpha", "v1", "v2", al)
    assert rep.full_rewrite is False
    assert any(c["table"] == "stops" for c in rep.changed)


def test_real_removal_and_addition_reported_with_evidence():
    old_t = good_tables()
    new_t = good_tables()
    new_t["stops"].pop(1)  # 物理删除南京路
    new_t["routes"][0]["stop_ids"].remove(old_t["stops"][1]["id"])
    new_t["stops"].append({"id": "S9", "name": "全新高铁站", "lat": 31.9,
                           "lon": 121.9, "valid_from": "2026-11-01",
                           "valid_to": "2027-12-31"})
    new_t["routes"][0]["stop_ids"].append("S9")
    al = align.align_stops(old_t, new_t, "alpha", "v1", "v2")
    rep = diff.diff_tables(old_t, new_t, "alpha", "v1", "v2", al)
    assert any(d["id"] == "S2" for d in rep.removed)
    assert any(d["id"] == "S9" for d in rep.added)


def test_field_level_change_has_before_after():
    old_t = good_tables()
    new_t = good_tables()
    new_t["stops"][0]["lat"] = 31.2310
    al = align.align_stops(old_t, new_t, "alpha", "v1", "v2")
    rep = diff.diff_tables(old_t, new_t, "alpha", "v1", "v2", al)
    ch = next(c for c in rep.changed if c["table"] == "stops")
    assert ch["changes"]["lat"]["before"] == 31.2304
    assert ch["changes"]["lat"]["after"] == 31.2310


def test_full_rewrite_without_alignment_requires_high_churn():
    old_t = good_tables()
    new_t = good_tables(lambda i: f"X{i}")
    for s in new_t["stops"]:
        s["name"] = "无关站" + s["id"]
        s["lat"], s["lon"] = 39.9, 116.4
    rep = diff.diff_tables(old_t, new_t, "alpha", "v1", "v2", None)
    assert rep.full_rewrite is True
