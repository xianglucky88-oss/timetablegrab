"""单一职责：版本状态机、生效切换规则与版本隔离查询。

确定规则：
- 新版本只有“完整下载 + 校验通过”后才能登记为 staged；残缺/阻断版本只能 rejected。
- 未来版（effective_date 晚于当前日期）保持 staged，到日由 activate_due 生效。
- 同一生效日只允许一个激活版本：先入 staged 者占位，后来同日冲突版本拒绝并留痕。
- 撤回（withdraw）只对尚未生效的版本生效；已生效版本撤回走替换流程，旧版可读。
- 激活切换以单事务完成并写激活日志；中断后 recover 依据日志恢复到确定状态。
- 查询严格按版本隔离，绝不拼接两个版本的数据。
"""
from __future__ import annotations
from datetime import date

from . import config, store
from .models import VersionConflict


def process(record):
    """遗留通用记录流水线入口（保持向后兼容）。"""
    rec = dict(record)
    rec.setdefault("_stage_version", "naive")
    return rec


def register(con, source, token, effective_date, published_at, manifest: dict,
             validation_report: dict, tables: dict, allowed_future_days: int = 365):
    """登记一个已完整校验的版本。校验阻断的版本由调用方登记为 rejected。"""
    today = date.today().isoformat()
    if effective_date < today:
        raise VersionConflict(
            f"版本 {token} 生效日 {effective_date} 已过，禁止回溯生效")
    if effective_date > _shift(today, allowed_future_days):
        raise VersionConflict(
            f"版本 {token} 生效日 {effective_date} 超出未来窗口 {allowed_future_days} 天")
    existing = store.get_feed_version(con, source, token)
    if existing and existing["state"] in (config.ST_ACTIVE, config.ST_STAGED):
        return existing["state"]
    store.upsert_feed_version(con, source, token, effective_date, published_at,
                              config.ST_STAGED, manifest, validation_report)
    store.save_validation_report(con, source, token, validation_report)
    for table_name, rows in tables.items():
        store.replace_version_rows(con, source, token, table_name, rows)
    store.write_journal(con, source, token, "staged",
                        {"effective_date": effective_date})
    # 同日冲突检测：同一生效日只允许一个待激活版本
    conflict = store.staged_for_date(con, source, effective_date)
    if conflict and conflict != token:
        store.set_state(con, source, token, config.ST_CANCELED)
        store.write_journal(con, source, token, "same_day_conflict",
                            {"winner": conflict, "effective_date": effective_date})
        con.commit()
        raise VersionConflict(
            f"生效日 {effective_date} 已有待激活版本 {conflict}，版本 {token} 取消")
    # 登记只负责 staged；未来版/到日激活由 activate_due 按确定规则处理
    con.commit()
    return config.ST_STAGED


def register_rejected(con, source, token, effective_date, published_at,
                      manifest: dict, validation_report: dict):
    """残缺或校验阻断版本：登记 rejected 并保留依据，永不进入激活。"""
    store.upsert_feed_version(con, source, token, effective_date, published_at,
                              config.ST_REJECTED, manifest, validation_report)
    store.save_validation_report(con, source, token, validation_report)
    store.write_journal(con, source, token, "rejected")
    con.commit()
    return config.ST_REJECTED


def activate_due(con, source, today: str | None = None) -> str | None:
    """激活所有到日的 staged 版本（每来源至多一个，同日冲突已在登记时拦截）。"""
    today = today or date.today().isoformat()
    row = con.execute(
        "SELECT version_token FROM feed_versions WHERE feed_source=? AND state='staged' "
        "AND effective_date<=? ORDER BY effective_date, published_at LIMIT 1",
        (source, today)).fetchone()
    if not row:
        return None
    activate(con, source, row[0], today=today)
    return row[0]  # 返回被激活的版本令牌，而非状态字符串


def activate(con, source, token, today: str | None = None) -> str:
    today = today or date.today().isoformat()
    meta = store.get_feed_version(con, source, token)
    if meta is None:
        raise VersionConflict(f"未知版本: {token}")
    if meta["state"] == config.ST_ACTIVE:
        return config.ST_ACTIVE
    if meta["state"] != config.ST_STAGED:
        raise VersionConflict(
            f"版本 {token} 状态为 {meta['state']}，只有 staged 可激活")
    if meta["effective_date"] > today:
        raise VersionConflict(
            f"未来版 {token}（生效日 {meta['effective_date']}）不得提前激活")

    # 单事务原子切换：旧 active -> superseded，新 staged -> active，日志先行
    store.write_journal(con, source, token, "activating", {"today": today})
    con.execute(
        "UPDATE feed_versions SET state='superseded' "
        "WHERE feed_source=? AND state='active'", (source,))
    store.set_state(con, source, token, config.ST_ACTIVE)
    store.write_journal(con, source, token, "activated", {"today": today})
    con.commit()
    return config.ST_ACTIVE


def withdraw(con, source, token, reason: str = "") -> str:
    """撤回：staged 直接 withdrawn；active 撤回后回退到最近一个历史版本。"""
    meta = store.get_feed_version(con, source, token)
    if meta is None:
        raise VersionConflict(f"未知版本: {token}")
    if meta["state"] in (config.ST_WITHDRAWN, config.ST_SUPERSEDED,
                         config.ST_REJECTED, config.ST_CANCELED):
        return meta["state"]
    store.write_journal(con, source, token, "withdrawing", {"reason": reason})
    if meta["state"] == config.ST_STAGED:
        store.set_state(con, source, token, config.ST_WITHDRAWN)
        store.write_journal(con, source, token, "withdrawn", {"reason": reason})
        con.commit()
        return config.ST_WITHDRAWN

    # active 撤回：用最近一个 superseded 版本接替，保证查询始终有确定版本
    fallback = con.execute(
        "SELECT version_token FROM feed_versions WHERE feed_source=? "
        "AND state='superseded' ORDER BY effective_date DESC LIMIT 1",
        (source,)).fetchone()
    store.set_state(con, source, token, config.ST_WITHDRAWN)
    if fallback:
        store.set_state(con, source, fallback[0], config.ST_ACTIVE)
        store.write_journal(con, source, fallback[0], "reactivated",
                            {"replaced_withdrawn": token})
    store.write_journal(con, source, token, "withdrawn",
                        {"reason": reason, "fallback": fallback[0] if fallback else None})
    con.commit()
    return config.ST_WITHDRAWN


def recover(con, source) -> dict:
    """中断恢复：依据激活日志把半完成的切换收敛到确定状态。

    activating 之后没有 activated -> 回滚为 staged（旧版仍为 active，未被破坏）；
    withdrawing 之后没有 withdrawn -> 完成撤回。
    """
    last = store.last_journal(con, source)
    if not last:
        return {"recovered": False, "reason": "no journal"}
    token, phase = last["version_token"], last["phase"]
    meta = store.get_feed_version(con, source, token)
    action = "none"
    if phase == "activating" and meta and meta["state"] == config.ST_STAGED:
        # 事务原子性下通常不可见；此处显式收敛，保证重启后行为确定
        store.write_journal(con, source, token, "recover_rollback_activate")
        action = "rollback_activate"
    elif phase == "withdrawing" and meta and meta["state"] == config.ST_ACTIVE:
        store.set_state(con, source, token, config.ST_WITHDRAWN)
        store.write_journal(con, source, token, "recover_finish_withdraw")
        action = "finish_withdraw"
    else:
        return {"recovered": False, "reason": "state already consistent",
                "last_phase": phase}
    con.commit()
    return {"recovered": True, "action": action, "version_token": token}


# ---- 版本隔离查询：只从一个版本取数，绝不跨版本拼接 ----
def query_table(con, source, table_name, token: str | None = None,
                on_date: str | None = None):
    token = token or store.active_version(con, source, on_date)
    if token is None:
        return []
    return store.version_rows(con, source, token, table_name) or []


def query_stops(con, source, token: str | None = None, on_date: str | None = None):
    return query_table(con, source, "stops", token, on_date)


def query_routes(con, source, token: str | None = None, on_date: str | None = None):
    return query_table(con, source, "routes", token, on_date)


def query_trips(con, source, token: str | None = None, on_date: str | None = None):
    return query_table(con, source, "trips", token, on_date)


def _shift(iso_day: str, days: int) -> str:
    from datetime import timedelta
    return (date.fromisoformat(iso_day) + timedelta(days=days)).isoformat()
