"""单一职责：派生稳定主键。"""
import hashlib, json

def key_of(record):
    rid = record.get("id")
    if rid:
        return str(rid)
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
