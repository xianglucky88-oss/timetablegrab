"""单一职责：派生稳定主键（基线=原始 id；跨源身份归并属功能开发缺口）。"""
import hashlib, json

def key_of(record):
    rid = record.get("id")
    if rid:
        return str(rid)
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
