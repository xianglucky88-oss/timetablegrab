"""单一职责：字段规整/规范化（对记录做单一职责变换：parse）。规范阶段。"""
def process(record):
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    rec["value"] = int(rec.get("value", 0))
    return rec
