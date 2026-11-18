"""跨源对齐：ID 漂移、低置信、拆分/合并、坐标/名称/方向/日期证据。"""
import pytest

from timetablegrab import align
from timetablegrab.config import ALIGN_HIGH, ALIGN_MEDIUM

from feedbuilders import good_tables


def _stop(sid, name, lat=31.23, lon=121.47, direction="",
          vf="2026-01-01", vt="2027-12-31"):
    return {"id": sid, "name": name, "lat": lat, "lon": lon,
            "direction": direction, "valid_from": vf, "valid_to": vt}


def test_id_drift_with_same_name_and_coord_maps_high_confidence():
    old = {"stops": [_stop("OLD-1", "人民广场", 31.2304, 121.4737)]}
    new = {"stops": [_stop("NEW-9", "人民广场", 31.2304, 121.4737)]}
    rep = align.align_stops(old, new, "alpha", "v1", "v2")
    item = next(i for i in rep.items if i.old_id == "OLD-1")
    assert item.new_id == "NEW-9" and item.confidence >= ALIGN_HIGH
    assert rep.mapping() == {"OLD-1": "NEW-9"}


def test_low_confidence_match_is_not_auto_applied():
    old = {"stops": [_stop("OLD-1", "人民广场", 31.2304, 121.4737)]}
    # 名称部分相似、坐标 5km 外、方向相反、日期不重叠 -> 低置信
    new = {"stops": [_stop("NEW-9", "人民公园", 31.30, 121.55,
                           direction="west", vf="2030-01-01", vt="2031-01-01")]}
    rep = align.align_stops(old, new, "alpha", "v1", "v2")
    assert rep.mapping() == {}  # 不自动换 ID
    none_item = next(i for i in rep.items if i.old_id == "OLD-1")
    assert none_item.method == "none" and none_item.evidence


def test_split_stop_kept_as_evidence_without_auto_mapping():
    old = {"stops": [_stop("OLD-1", "人民广场", 31.2304, 121.4737)]}
    new = {"stops": [
        _stop("N1", "人民广场东", 31.2306, 121.4740),
        _stop("N2", "人民广场西", 31.2302, 121.4730),
    ]}
    rep = align.align_stops(old, new, "alpha", "v1", "v2")
    splits = [i for i in rep.items if i.method == "split"]
    assert {i.new_id for i in splits} == {"N1", "N2"}
    assert all(i.evidence.get("family") for i in splits)
    assert rep.mapping() == {}  # 拆分不自动迁移


def test_unmatched_ids_recorded_not_silently_dropped():
    old = {"stops": [_stop("OLD-1", "甲站")]}
    new = {"stops": [_stop("NEW-1", "完全陌生站", 39.9, 116.4)]}
    rep = align.align_stops(old, new, "alpha", "v1", "v2")
    methods = {(i.old_id, i.new_id, i.method) for i in rep.items}
    assert ("OLD-1", "", "none") in methods
    assert ("", "NEW-1", "none") in methods


def test_name_only_match_with_missing_coord_still_scored():
    old = {"stops": [{"id": "A", "name": "南京路",
                      "valid_from": "2026-01-01", "valid_to": "2027-12-31"}]}
    new = {"stops": [{"id": "B", "name": "南京路",
                      "valid_from": "2026-01-01", "valid_to": "2027-12-31"}]}
    rep = align.align_stops(old, new, "alpha", "v1", "v2")
    item = rep.items[0]
    assert item.method == "fuzzy" and item.confidence >= ALIGN_MEDIUM
    assert item.evidence["coord"] == 0.0  # 缺坐标证据透明可见


def test_ambiguous_candidates_downgraded():
    old = {"stops": [_stop("A", "中山公园", 31.23, 121.47)]}
    new = {"stops": [
        _stop("B1", "中山公园", 31.2301, 121.4701),
        _stop("B2", "中山公园", 31.2302, 121.4702),  # 与 B1 几乎同质量
    ]}
    rep = align.align_stops(old, new, "alpha", "v1", "v2")
    # 两个同质量候选被识别为拆分族，全部不自动换 ID，并保留依据
    assert rep.mapping() == {}
    family = [i for i in rep.items if i.method == "split"]
    assert {i.new_id for i in family} == {"B1", "B2"}
    assert all(i.evidence.get("family") == "中山公园" for i in family)


def test_two_close_non_family_candidates_leave_manual_review():
    old = {"stops": [_stop("A", "中山路", 31.23, 121.47)]}
    new = {"stops": [
        _stop("B1", "中山道", 31.2300, 121.4700),
        _stop("B2", "中山街", 31.2300, 121.4700),  # 名称不同族、得分接近
    ]}
    rep = align.align_stops(old, new, "alpha", "v1", "v2")
    # 非同名族且存在强歧义时不自动配对，旧站以 none 留痕
    assert rep.mapping() == {}
    assert any(i.old_id == "A" and i.method == "none" for i in rep.items)
