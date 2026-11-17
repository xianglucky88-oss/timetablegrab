"""单一职责：对记录做单一职责变换：version。

用于记录变换。"""
def process(record):
    rec = dict(record)
    rec.setdefault("_stage_version", "naive")
    return rec
