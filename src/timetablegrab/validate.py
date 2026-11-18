"""单一职责：结构与业务规则校验，明确阻断/警告/可修复三类界线。

严重级别约定：
- BLOCKER  数据不可自动修复，整包不得进入生效流程（残缺版本不会被激活）。
- WARNING  数据可进入流程，但必须保留告警依据供人工复核。
- REPAIRED 确定性的安全自动修复（去空白、去连续重复停靠、补默认值），
           原值与修复后值都记录在案。

覆盖规则：重复键、外键完整性、停靠顺序（含跨日班次 HH:MM:SS >24 时制）、
服务日期与日历、坐标合法性、资源上限保护。
"""
from __future__ import annotations
from datetime import date

from . import config
from .models import Finding, Row, ValidationError, ValidationReport

TABLES = ("stops", "routes", "trips", "stop_times", "calendar")


def process(record):
    """遗留通用记录流水线入口（校验阶段未接入旧链，仅保留可调用占位）。"""
    rec = dict(record)
    rec.setdefault("_stage_validate", "naive")
    return rec


def _norm_id(value) -> str:
    return str(value if value is not None else "").strip()


def _parse_clock(value) -> int | None:
    """GTFS 风格时刻 -> 从当日 00:00 起的分钟数；支持 24:00:00 跨日表示。"""
    if value is None:
        return None
    parts = str(value).strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    if not (0 <= m < 60 and 0 <= s < 60) or h < 0:
        return None
    return h * 60 + m + (1 if s >= 30 else 0)


def _iso_date(value) -> date | None:
    try:
        y, m, d = str(value).strip().split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def _group(rows: list[Row]) -> dict[str, list[Row]]:
    grouped: dict[str, list[Row]] = {t: [] for t in TABLES}
    for r in rows:
        grouped.setdefault(r.table, []).append(r)
    return grouped


def _emit(report: ValidationReport, severity, code, message, row: Row | None = None,
          entity_id: str = "") -> None:
    if len(report.findings) >= config.MAX_FINDINGS_PER_REPORT:
        return
    report.add(Finding(severity, code, message, table=row.table if row else "",
                       entity_id=entity_id or (
                           str(row.data.get("id", "")) if row else ""),
                       file=row.source_file if row else "",
                       line=row.line if row else None))


def validate(rows: list[Row], feed_source: str, version_token: str
             ) -> tuple[ValidationReport, dict[str, list[dict]]]:
    """返回 (报告, 规范化后的表数据)。出现 BLOCKER 时调用方必须阻断换版。"""
    report = ValidationReport(feed_source, version_token)
    grouped = _group(rows)
    clean: dict[str, list[dict]] = {t: [] for t in TABLES}

    # ---- stops：主键、坐标、名称修复 ----
    seen_stops: set[str] = set()
    for r in grouped.get("stops", []):
        d = dict(r.data)
        raw_id = d.get("id")
        sid = _norm_id(raw_id)
        if not sid:
            _emit(report, "BLOCKER", "MISSING_KEY", "站点缺少 id", r)
            continue
        if raw_id != sid:
            _emit(report, "REPAIRED", "ID_WHITESPACE",
                  f"站点 id 去除首尾空白: {raw_id!r} -> {sid!r}", r, sid)
        if sid in seen_stops:
            _emit(report, "BLOCKER", "DUPLICATE_KEY", f"站点 id 重复: {sid}", r, sid)
            continue
        seen_stops.add(sid)
        lat, lon = d.get("lat"), d.get("lon")
        try:
            lat_f, lon_f = float(lat), float(lon)
            if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
                raise ValueError
            d["lat"], d["lon"] = lat_f, lon_f
        except (TypeError, ValueError):
            _emit(report, "WARNING", "BAD_COORD",
                  f"站点 {sid} 坐标缺失或越界，对齐时仅能依赖名称", r, sid)
            d.pop("lat", None)
            d.pop("lon", None)
        name = d.get("name")
        if name is None or not str(name).strip():
            _emit(report, "BLOCKER", "MISSING_NAME", f"站点 {sid} 缺少名称", r, sid)
            continue
        if str(name) != str(name).strip():
            _emit(report, "REPAIRED", "NAME_WHITESPACE",
                  f"站点名称去除首尾空白: {name!r} -> {str(name).strip()!r}", r, sid)
        d["id"] = sid
        d["name"] = str(name).strip()
        clean["stops"].append(d)
    stop_ids = {d["id"] for d in clean["stops"]}

    # ---- routes：主键、名称、方向、站点外键 ----
    seen_routes: set[str] = set()
    for r in grouped.get("routes", []):
        d = dict(r.data)
        rid = _norm_id(d.get("id"))
        if not rid:
            _emit(report, "BLOCKER", "MISSING_KEY", "线路缺少 id", r)
            continue
        if rid in seen_routes:
            _emit(report, "BLOCKER", "DUPLICATE_KEY", f"线路 id 重复: {rid}", r, rid)
            continue
        seen_routes.add(rid)
        if not str(d.get("name", "")).strip():
            _emit(report, "BLOCKER", "MISSING_NAME", f"线路 {rid} 缺少名称", r, rid)
            continue
        direction = str(d.get("direction", "")).strip().lower()
        if direction and direction not in ("outbound", "inbound", "north", "south",
                                           "east", "west", "clockwise",
                                           "anticlockwise", "0", "1"):
            _emit(report, "WARNING", "UNKNOWN_DIRECTION",
                  f"线路 {rid} 方向取值非常规: {direction!r}", r, rid)
        stop_list = d.get("stop_ids") or []
        if len(stop_list) < 2:
            _emit(report, "BLOCKER", "ROUTE_TOO_SHORT",
                  f"线路 {rid} 至少需要两个停靠站", r, rid)
            continue
        missing = [s for s in (_norm_id(x) for x in stop_list) if s not in stop_ids]
        if missing:
            _emit(report, "BLOCKER", "FOREIGN_KEY",
                  f"线路 {rid} 引用不存在的站点: {missing[:5]}", r, rid)
            continue
        d["id"] = rid
        d["name"] = str(d["name"]).strip()
        d["stop_ids"] = [_norm_id(x) for x in stop_list]
        clean["routes"].append(d)
    route_ids = {d["id"] for d in clean["routes"]}

    # ---- trips：主键、线路外键、日历外键 ----
    seen_trips: set[str] = set()
    for r in grouped.get("trips", []):
        d = dict(r.data)
        tid = _norm_id(d.get("id"))
        if not tid:
            _emit(report, "BLOCKER", "MISSING_KEY", "班次缺少 id", r)
            continue
        if tid in seen_trips:
            _emit(report, "BLOCKER", "DUPLICATE_KEY", f"班次 id 重复: {tid}", r, tid)
            continue
        seen_trips.add(tid)
        rid = _norm_id(d.get("route_id"))
        if rid not in route_ids:
            _emit(report, "BLOCKER", "FOREIGN_KEY",
                  f"班次 {tid} 引用不存在的线路: {rid!r}", r, tid)
            continue
        service = _norm_id(d.get("service_id"))
        if service and service not in {_norm_id(x.data.get("id"))
                                       for x in grouped.get("calendar", [])}:
            _emit(report, "BLOCKER", "FOREIGN_KEY",
                  f"班次 {tid} 引用不存在的服务日历: {service!r}", r, tid)
            continue
        d["id"], d["route_id"] = tid, rid
        if service:
            d["service_id"] = service
        clean["trips"].append(d)
    trip_ids = {d["id"] for d in clean["trips"]}

    # ---- stop_times：外键、停靠顺序、跨日时刻 ----
    by_trip: dict[str, list[tuple[Row, dict]]] = {}
    for r in grouped.get("stop_times", []):
        d = dict(r.data)
        tid = _norm_id(d.get("trip_id"))
        if tid not in trip_ids:
            _emit(report, "BLOCKER", "FOREIGN_KEY",
                  f"停靠时刻引用不存在的班次: {tid!r}", r)
            continue
        sid = _norm_id(d.get("stop_id"))
        if sid not in stop_ids:
            _emit(report, "BLOCKER", "FOREIGN_KEY",
                  f"班次 {tid} 停靠不存在的站点: {sid!r}", r, tid)
            continue
        arr, dep = _parse_clock(d.get("arrival")), _parse_clock(d.get("departure"))
        if arr is None or dep is None:
            _emit(report, "BLOCKER", "BAD_TIME",
                  f"班次 {tid} 在 {sid} 的时刻非法: "
                  f"{d.get('arrival')!r}/{d.get('departure')!r}", r, tid)
            continue
        if dep < arr:
            _emit(report, "BLOCKER", "BAD_TIME",
                  f"班次 {tid} 在 {sid} 发车早于到达", r, tid)
            continue
        try:
            seq = int(d.get("stop_sequence"))
            if seq < 0:
                raise ValueError
        except (TypeError, ValueError):
            _emit(report, "BLOCKER", "BAD_SEQUENCE",
                  f"班次 {tid} 的 stop_sequence 非法: {d.get('stop_sequence')!r}",
                  r, tid)
            continue
        d.update({"trip_id": tid, "stop_id": sid, "stop_sequence": seq,
                  "arrival_min": arr, "departure_min": dep})
        by_trip.setdefault(tid, []).append((r, d))

    for tid, items in by_trip.items():
        # 同序号时按到达时刻稳定排序，保证连续重复停靠先于正序行被识别
        items.sort(key=lambda x: (x[1]["stop_sequence"],
                                  x[1]["arrival_min"], x[1]["departure_min"]))
        # 第一遍：仅删除连续重复停靠（可修复），避免把重复行误判成顺序倒退
        deduped: list[tuple[Row, dict]] = []
        last_stop = None
        for r, d in items:
            if d["stop_id"] == last_stop:
                _emit(report, "REPAIRED", "DUP_STOP",
                      f"班次 {tid} 连续重复停靠 {last_stop}，已删除冗余行",
                      r, tid)
                continue
            deduped.append((r, d))
            last_stop = d["stop_id"]

        ordered: list[dict] = []
        last_dep, last_stop = None, None
        for r, d in deduped:
            if last_dep is not None and d["arrival_min"] < last_dep:
                _emit(report, "BLOCKER", "STOP_ORDER",
                      f"班次 {tid} 停靠顺序倒退: {last_stop}@{last_dep} -> "
                      f"{d['stop_id']}@{d['arrival_min']}", r, tid)
                continue
            ordered.append(d)
            last_dep, last_stop = d["departure_min"], d["stop_id"]
        # 原始序列中的重复编号（含已删除的连续重复停靠）统一重排为连续整数
        seqs = [d["stop_sequence"] for d in ordered]
        if len(seqs) != len(set(seqs)) or seqs != sorted(seqs) or \
                (seqs and seqs[0] != 1) or (seqs and list(seqs) != list(range(1, len(seqs) + 1))):
            _emit(report, "REPAIRED", "SEQUENCE_RENUMBER",
                  f"班次 {tid} 停靠序列不连续/重复，已重排为 1..{len(ordered)}",
                  entity_id=tid)
            for i, d in enumerate(ordered, start=1):
                d["stop_sequence"] = i
        if len(ordered) < 2:
            _emit(report, "BLOCKER", "ROUTE_TOO_SHORT",
                  f"班次 {tid} 有效停靠点不足两个", entity_id=tid)
            continue
        cross_midnight = ordered[-1]["arrival_min"] >= 24 * 60
        if cross_midnight:
            _emit(report, "WARNING", "CROSS_MIDNIGHT",
                  f"班次 {tid} 跨日运行（到达 {ordered[-1]['arrival_min'] // 60} 时），"
                  f"服务日期按出发日归属", entity_id=tid)
        clean["stop_times"].extend(ordered)

    # ---- calendar：日期窗口与星期位图 ----
    cal_ids: set[str] = set()
    for r in grouped.get("calendar", []):
        d = dict(r.data)
        cid = _norm_id(d.get("id"))
        if not cid:
            _emit(report, "BLOCKER", "MISSING_KEY", "日历缺少 id", r)
            continue
        if cid in cal_ids:
            _emit(report, "BLOCKER", "DUPLICATE_KEY", f"日历 id 重复: {cid}", r, cid)
            continue
        start, end = _iso_date(d.get("start_date")), _iso_date(d.get("end_date"))
        if start is None or end is None:
            _emit(report, "BLOCKER", "BAD_DATE",
                  f"日历 {cid} 日期非法: {d.get('start_date')!r}/{d.get('end_date')!r}",
                  r, cid)
            continue
        if end < start:
            _emit(report, "BLOCKER", "BAD_DATE",
                  f"日历 {cid} end_date 早于 start_date", r, cid)
            continue
        weekdays = d.get("weekdays")
        if weekdays is not None and (
            not isinstance(weekdays, list)
            or any(x not in range(7) for x in weekdays)
        ):
            _emit(report, "REPAIRED", "WEEKDAY_DEFAULT",
                  f"日历 {cid} 星期位图非法，已回退为每日服务", r, cid)
            weekdays = list(range(7))
        d["id"], d["start_date"], d["end_date"] = cid, start.isoformat(), end.isoformat()
        if weekdays is not None:
            d["weekdays"] = sorted(set(weekdays))
        cal_ids.add(cid)
        clean["calendar"].append(d)

    # ---- 跨表服务日期：班次出发日必须落在日历窗口内 ----
    cal_by_id = {c["id"]: c for c in clean["calendar"]}
    for t in clean["trips"]:
        cid = t.get("service_id")
        if not cid:
            _emit(report, "WARNING", "NO_CALENDAR",
                  f"班次 {t['id']} 未绑定服务日历，按生效日内有效处理",
                  entity_id=t["id"])
            continue
        cal = cal_by_id.get(cid)
        if cal is None:
            continue  # 日历本身已记 BLOCKER，避免重复报错
        run_day = t.get("service_date") or cal["start_date"]
        run = _iso_date(run_day)
        if run is None:
            _emit(report, "BLOCKER", "BAD_DATE",
                  f"班次 {t['id']} service_date 非法: {run_day!r}", entity_id=t["id"])
        elif not (_iso_date(cal["start_date"]) <= run <= _iso_date(cal["end_date"])):
            _emit(report, "BLOCKER", "SERVICE_OUTSIDE_CALENDAR",
                  f"班次 {t['id']} 服务日 {run_day} 超出日历窗口 "
                  f"{cal['start_date']}..{cal['end_date']}", entity_id=t["id"])
        wd = cal_by_id[cid].get("weekdays")
        if run is not None and wd is not None and run.weekday() not in wd:
            _emit(report, "WARNING", "SERVICE_WEEKDAY_MISMATCH",
                  f"班次 {t['id']} 服务日 {run_day} 不在日历允许的星期位图内",
                  entity_id=t["id"])

    referenced_stops = {s for route in clean["routes"] for s in route["stop_ids"]}
    referenced_stops |= {st["stop_id"] for st in clean["stop_times"]}
    for s in clean["stops"]:
        if s["id"] not in referenced_stops:
            _emit(report, "WARNING", "UNUSED_STOP",
                  f"站点 {s['id']} 未被任何线路/班次引用", entity_id=s["id"])

    return report, clean


def validate_or_raise(rows, feed_source, version_token):
    """校验阻断时抛 ValidationError；否则返回规范化表数据。"""
    report, clean = validate(rows, feed_source, version_token)
    if report.blocked:
        raise ValidationError(report)
    return report, clean
