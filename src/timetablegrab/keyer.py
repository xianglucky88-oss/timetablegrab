"""单一职责：派生稳定主键。"""
from .hashutil import stable_key


def key_of(record):
    rid = record.get("id")
    if rid:
        return str(rid)
    return stable_key(record)


def row_key(table: str, row: dict) -> str:
    """时刻表行的稳定键：表名 + 业务 id（缺 id 时回退内容哈希）。"""
    rid = row.get("id") or row.get("trip_id") or row.get("stop_id")
    if rid:
        return f"{table}:{rid}"
    return f"{table}:{stable_key(row)}"
