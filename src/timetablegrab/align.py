"""单一职责：跨源/跨版本站点对齐，处理站点编号漂移（ID drift）。

ID 漂移不直接判定“删除+新增”：结合名称、坐标、方向、服务日期综合打分，
输出带置信度与依据的对应关系；低置信、拆分/合并对应保留依据但不自动换 ID。
"""
from __future__ import annotations
import math
from datetime import date

from . import config
from .models import AlignItem, AlignReport

# 名称、坐标、方向、日期四类证据的权重
W_NAME, W_COORD, W_DIRECTION, W_DATE = 0.45, 0.35, 0.10, 0.10


def process(record):
    """遗留通用记录流水线入口（保持向后兼容）。"""
    rec = dict(record)
    rec.setdefault("_stage_align", "ok")
    return rec


def normalize_name(name: str) -> str:
    return "".join(str(name).lower().split())


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _name_score(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.85
    # 字符集合 Jaccard，容忍“站/广场/东”等后缀差异
    sa, sb = set(na), set(nb)
    inter = len(sa & sb)
    return 0.6 * inter / len(sa | sb)


def _coord_score(a: dict, b: dict) -> tuple[float, float | None]:
    try:
        dist = _haversine_m(float(a["lat"]), float(a["lon"]),
                            float(b["lat"]), float(b["lon"]))
    except (KeyError, TypeError, ValueError):
        return 0.0, None
    if dist <= config.COORD_NEAR_M:
        return 1.0, dist
    if dist <= config.COORD_MID_M:
        return 0.6, dist
    if dist <= config.COORD_FAR_M:
        return 0.2, dist
    return 0.0, dist


def _direction_score(a: dict, b: dict) -> float:
    da = str(a.get("direction", "")).lower()
    db = str(b.get("direction", "")).lower()
    if not da or not db:
        return 0.5  # 方向缺失时保持中性，不因缺证据惩罚
    return 1.0 if da == db else 0.0


def _date_windows_overlap(a: dict, b: dict) -> float:
    def span(d):
        try:
            return (date.fromisoformat(str(d["valid_from"])),
                    date.fromisoformat(str(d["valid_to"])))
        except (KeyError, ValueError, TypeError):
            return None
    sa, sb = span(a), span(b)
    if sa is None or sb is None:
        return 0.5  # 日期证据缺失同样保持中性
    return 1.0 if max(sa[0], sb[0]) <= min(sa[1], sb[1]) else 0.0


def score_pair(old: dict, new: dict) -> tuple[float, dict]:
    ns = _name_score(old.get("name", ""), new.get("name", ""))
    cs, dist = _coord_score(old, new)
    ds = _direction_score(old, new)
    ts = _date_windows_overlap(old, new)
    total = W_NAME * ns + W_COORD * cs + W_DIRECTION * ds + W_DATE * ts
    return round(total, 4), {"name": round(ns, 3), "coord": round(cs, 3),
                             "coord_distance_m": None if dist is None else round(dist, 1),
                             "direction": ds, "date_overlap": ts}


def _route_direction_context(tables: dict, stop_id: str) -> str:
    for route in tables.get("routes", []):
        if stop_id in route.get("stop_ids", []):
            return str(route.get("direction", "")).lower()
    return ""


def align_stops(old_tables: dict, new_tables: dict,
                feed_source: str, old_version: str, new_version: str) -> AlignReport:
    old_stops = list(old_tables.get("stops", []))
    new_stops = list(new_tables.get("stops", []))
    report = AlignReport(feed_source, old_version, new_version)

    # 第一阶段：先识别拆分/合并同族站点，这些新 ID 退出一对一竞争
    reserved_new, split_old = _detect_splits_merges(report, old_stops, new_stops)
    available_new = [n for n in new_stops if str(n["id"]) not in reserved_new]

    # 第二阶段：对剩余站点贪心做一对一最佳匹配
    candidates: list[tuple[float, dict, dict, dict]] = []
    for o in old_stops:
        if str(o["id"]) in split_old:
            continue  # 已有拆分依据的旧站不再做一对一
        for n in available_new:
            score, evidence = score_pair(o, n)
            candidates.append((score, evidence, o, n))
    candidates.sort(key=lambda x: x[0], reverse=True)

    pair_by_old: dict[str, AlignItem] = {}
    pair_by_new: dict[str, AlignItem] = {}
    for score, evidence, o, n in candidates:
        oid, nid = str(o["id"]), str(n["id"])
        if oid in pair_by_old or nid in pair_by_new:
            continue
        # 次佳对手差距用于识别歧义对应
        rival = max((s for s, _, oo, nn in candidates
                     if str(oo["id"]) == oid and str(nn["id"]) != nid),
                    default=0.0)
        ambiguous = (score - rival) < config.ALIGN_AMBIGUITY_GAP
        if ambiguous or score < config.ALIGN_MEDIUM:
            continue  # 歧义/低于阈值：不自动配对，留到末尾记 none 依据
        method = "exact" if score >= config.ALIGN_HIGH else "fuzzy"
        item = AlignItem(oid, nid, score, method,
                         {**evidence, "ambiguous": ambiguous,
                          "old_name": o.get("name"), "new_name": n.get("name")})
        pair_by_old[oid] = item
        pair_by_new[nid] = item
        report.items.append(item)

    # 第三阶段：未配对的旧站/新站记录 none 依据，绝不静默删除
    matched_old = {i.old_id for i in report.items}
    for o in old_stops:
        if str(o["id"]) not in matched_old:
            report.items.append(AlignItem(
                str(o["id"]), "", 0.0, "none",
                {"reason": "no candidate above threshold", "old_name": o.get("name")}))
    matched_new = {i.new_id for i in report.items if i.new_id}
    for n in new_stops:
        if str(n["id"]) not in matched_new:
            report.items.append(AlignItem(
                "", str(n["id"]), 0.0, "none",
                {"reason": "unmatched new id, kept for manual review",
                 "new_name": n.get("name")}))
    return report


def _detect_splits_merges(report: AlignReport, old_stops, new_stops):
    """返回 (被拆分/合并占用的新 ID 集合, 已有拆分依据的旧 ID 集合)。"""
    reserved_new: set[str] = set()
    split_old: set[str] = set()
    old_by_norm = {normalize_name(o.get("name", "")): o for o in old_stops}
    new_groups: dict[str, list[dict]] = {}
    for n in new_stops:
        key = normalize_name(n.get("name", ""))
        root = next((k for k in old_by_norm if k and (k in key or key in k)), None)
        if root:
            new_groups.setdefault(root, []).append(n)
    for root, group in new_groups.items():
        o = old_by_norm.get(root)
        if o is None or len(group) < 2:
            continue
        oid = str(o["id"])
        split_old.add(oid)
        for n in group:
            score, evidence = score_pair(o, n)
            report.items.append(AlignItem(
                oid, str(n["id"]), round(score * 0.7, 4), "split",
                {**evidence, "family": root,
                 "note": "一站拆多站，保留依据，不自动迁移 ID"}))
            reserved_new.add(str(n["id"]))
    return reserved_new, split_old
