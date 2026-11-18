"""单一职责：换版差分。ID 漂移经 align 映射后识别为“变更”而非“删除+新增”。

- 站点：先按对齐映射配对，未配对的删/增必须有低置信依据，不触发整表重写误报。
- 线路/班次：用 (route_id/自然键) 配对，字段级变更给出 before/after。
- 整表重写判定：只有无对齐依据且删增占比超过 FULL_REWRITE_RATIO 才成立。
"""
from __future__ import annotations
from . import config
from .models import AlignReport, DiffReport

_STOP_FIELDS = ("name", "lat", "lon", "zone_id")
_ROUTE_FIELDS = ("name", "direction", "stop_ids")
_TRIP_FIELDS = ("route_id", "service_id", "service_date")


def process(record):
    """遗留通用记录流水线入口（保持向后兼容）。"""
    rec = dict(record)
    rec.setdefault("_stage_diff", "naive")
    return rec


def _field_changes(old: dict, new: dict, fields: tuple[str, ...]) -> dict:
    changes = {}
    for f in fields:
        ov, nv = old.get(f), new.get(f)
        if ov != nv:
            changes[f] = {"before": ov, "after": nv}
    return changes


def _natural_key(row: dict) -> tuple:
    """无稳定 id 时的自然键：名称 + 站点序列/服务日，避免误判整表新增。"""
    return tuple(str(row.get(k, "")).strip().lower()
                 for k in ("name", "route_id", "service_date"))


def diff_tables(old_tables: dict, new_tables: dict,
                feed_source: str, base_version: str, target_version: str,
                alignment: AlignReport | None = None) -> DiffReport:
    report = DiffReport(feed_source, base_version, target_version)
    id_map = alignment.mapping() if alignment else {}
    low_conf = {i.old_id: i for i in (alignment.items if alignment else [])
                if i.method in ("none", "split", "merge")}

    old_stops = {str(s["id"]): s for s in old_tables.get("stops", [])}
    new_stops = {str(s["id"]): s for s in new_tables.get("stops", [])}

    for oid, o in old_stops.items():
        nid = id_map.get(oid)
        if nid and nid in new_stops:
            changes = _field_changes(o, new_stops[nid], _STOP_FIELDS)
            if changes:
                report.changed.append({"table": "stops", "old_id": oid,
                                       "new_id": nid, "changes": changes,
                                       "id_drift": nid != oid})
            if nid != oid:
                report.id_drift.append({"table": "stops", "old_id": oid,
                                        "new_id": nid,
                                        "confidence": next(
                                            (i.confidence for i in alignment.items
                                             if i.old_id == oid and i.new_id == nid),
                                            0.0)})
        else:
            entry = {"table": "stops", "id": oid, "name": o.get("name"),
                     "evidence": low_conf.get(oid).evidence if low_conf.get(oid) else None}
            report.removed.append(entry)

    mapped_new = set(id_map.values())
    for nid, n in new_stops.items():
        if nid in mapped_new or nid in old_stops:
            continue
        report.added.append({"table": "stops", "id": nid, "name": n.get("name"),
                             "evidence": next(
                                 (i.evidence for i in alignment.items
                                  if i.new_id == nid and i.method == "none"),
                                 None) if alignment else None})

    for table, fields, keyfield in (("routes", _ROUTE_FIELDS, "id"),
                                    ("trips", _TRIP_FIELDS, "id")):
        olds = {str(r[keyfield]): r for r in old_tables.get(table, [])}
        news = {str(r[keyfield]): r for r in new_tables.get(table, [])}
        for rid, o in olds.items():
            if rid in news:
                changes = _field_changes(o, news[rid], fields)
                if changes:
                    report.changed.append({"table": table, "id": rid,
                                           "changes": changes})
            else:
                # 自然键回退：编号变了但业务实体仍在，不算物理删除
                match = next((n for n in news.values()
                              if _natural_key(n) == _natural_key(o)), None)
                if match is not None:
                    report.id_drift.append({"table": table, "old_id": rid,
                                            "new_id": str(match[keyfield]),
                                            "confidence": config.ALIGN_MEDIUM})
                    changes = _field_changes(o, match, fields)
                    if changes:
                        report.changed.append({"table": table, "old_id": rid,
                                               "new_id": str(match[keyfield]),
                                               "changes": changes, "id_drift": True})
                else:
                    report.removed.append({"table": table, "id": rid,
                                           "name": o.get("name")})
        for nid, n in news.items():
            if nid not in olds and not any(
                    d.get("table") == table and d.get("new_id") == nid
                    for d in report.id_drift):
                report.added.append({"table": table, "id": nid,
                                     "name": n.get("name")})

    total_old = sum(len(old_tables.get(t, [])) for t in ("stops", "routes", "trips"))
    churn = len(report.added) + len(report.removed)
    # 有对齐依据时禁止整表重写误报；无依据且绝大多数 id 同时消失/出现才判定重写
    report.full_rewrite = (
        total_old > 0 and not alignment
        and churn >= config.FULL_REWRITE_RATIO * total_old
    )
    report.summary = {
        "added": len(report.added),
        "removed": len(report.removed),
        "changed": len(report.changed),
        "id_drift": len(report.id_drift),
        "full_rewrite": int(report.full_rewrite),
    }
    return report
