"""解析与校验：行定位、重复键、外键、停靠顺序、跨日班次、服务日期、修复界线。"""
import json

import pytest

from timetablegrab import parse, validate
from timetablegrab.models import Finding, Row, ValidationError

from feedbuilders import good_tables


def _rows(tables):
    out = []
    for t, rs in tables.items():
        for i, d in enumerate(rs, start=1):
            out.append(Row(t, d, f"{t}.json", i))
    return out


def test_jsonl_parse_keeps_line_position(tmp_path):
    p = tmp_path / "stops.jsonl"
    p.write_text('{"id":"S1","name":"站"}\n\nbroken-line\n{"id":"S2"}\n',
                 encoding="utf-8")
    r = parse.parse_shard(p, "stops", "jsonl")
    assert r.ok is False and r.blocked
    err = next(f for f in r.findings if f.code == "PARSE_ERROR")
    assert err.line == 3 and err.file == "stops.jsonl"
    assert [row.data["id"] for row in r.rows] == ["S1", "S2"]


def test_json_parse_error_line(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('[{"id": "S1"}, {"id": broken}]', encoding="utf-8")
    r = parse.parse_shard(p, "stops", "json")
    assert r.blocked and r.findings[0].code == "PARSE_ERROR"
    assert r.findings[0].line == 1


def test_row_limit_is_blocker(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"id":1}\n{"id":2}\n', encoding="utf-8")
    r = parse.parse_shard(p, "stops", "jsonl", max_rows=1)
    assert r.blocked and r.findings[0].code == "RESOURCE_LIMIT"


def test_clean_package_passes_with_repairs_and_warnings():
    tables = good_tables()
    # 可修复：id/name 空白、连续重复停靠
    tables["stops"][0]["id"] = "  S1 "
    tables["stops"][0]["name"] = "  人民广场 "
    tables["stop_times"].insert(1, dict(tables["stop_times"][0]))
    report, clean = validate.validate(_rows(tables), "alpha", "v1")
    assert not report.blocked
    codes = {f.code for f in report.findings}
    assert {"ID_WHITESPACE", "NAME_WHITESPACE", "DUP_STOP"} <= codes
    assert clean["stops"][0]["id"] == "S1"
    # 连续重复停靠被删除，T1 仍保留 3 个有效停靠点
    t1 = [r for r in clean["stop_times"] if r["trip_id"] == "T1"]
    assert len(t1) == 3


def test_duplicate_and_foreign_keys_are_blockers():
    tables = good_tables()
    tables["stops"].append(dict(tables["stops"][0]))  # 重复主键
    tables["routes"][0]["stop_ids"].append("GHOST")   # 外键悬空
    report, _ = validate.validate(_rows(tables), "alpha", "v1")
    codes = {f.code for f in report.findings if f.severity == "BLOCKER"}
    assert "DUPLICATE_KEY" in codes and "FOREIGN_KEY" in codes


def test_stop_order_regression_is_blocker():
    tables = good_tables()
    tables["stop_times"][2]["arrival"] = "07:00:00"  # 早于前序 08:10
    tables["stop_times"][2]["departure"] = "07:05:00"
    report, _ = validate.validate(_rows(tables), "alpha", "v1")
    assert any(f.code == "STOP_ORDER" and f.severity == "BLOCKER"
               for f in report.findings)


def test_cross_midnight_trip_is_warning_not_blocker():
    tables = good_tables()
    tables["stop_times"][2]["arrival"] = "25:30:00"   # 次日 01:30
    tables["stop_times"][2]["departure"] = "25:31:00"
    report, clean = validate.validate(_rows(tables), "alpha", "v1")
    assert not report.blocked
    assert any(f.code == "CROSS_MIDNIGHT" and f.severity == "WARNING"
               for f in report.findings)
    assert clean["stop_times"][-1]["arrival_min"] == 25 * 60 + 30


def test_service_date_outside_calendar_is_blocker():
    tables = good_tables()
    tables["trips"][0]["service_date"] = "2027-06-01"  # 日历止于 2026-12-31
    report, _ = validate.validate(_rows(tables), "alpha", "v1")
    assert any(f.code == "SERVICE_OUTSIDE_CALENDAR"
               and f.severity == "BLOCKER" for f in report.findings)


def test_weekday_mismatch_is_warning():
    tables = good_tables()
    tables["calendar"][0]["weekdays"] = [0]  # 仅周一；2026-12-01 是周二
    report, _ = validate.validate(_rows(tables), "alpha", "v1")
    assert any(f.code == "SERVICE_WEEKDAY_MISMATCH"
               and f.severity == "WARNING" for f in report.findings)


def test_bad_calendar_window_is_blocker():
    tables = good_tables()
    tables["calendar"][0]["end_date"] = "2025-01-01"
    report, _ = validate.validate(_rows(tables), "alpha", "v1")
    assert any(f.code == "BAD_DATE" and f.severity == "BLOCKER"
               for f in report.findings)


def test_validate_or_raise_carries_report():
    tables = good_tables()
    tables["stops"][1]["name"] = "   "  # 缺名称 -> BLOCKER
    with pytest.raises(ValidationError) as ei:
        validate.validate_or_raise(_rows(tables), "alpha", "v1")
    assert ei.value.report.blocked


def test_unused_stop_is_warning():
    tables = good_tables()
    tables["stops"].append({"id": "SX", "name": "备用站", "lat": 31.0,
                            "lon": 121.0, "valid_from": "2026-01-01",
                            "valid_to": "2026-12-31"})
    report, _ = validate.validate(_rows(tables), "alpha", "v1")
    assert any(f.code == "UNUSED_STOP" and f.severity == "WARNING"
               for f in report.findings)
