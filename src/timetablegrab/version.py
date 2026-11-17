"""单一职责：对记录做单一职责变换：version。

[功能优化目标] 以下为朴素但可运行的实现，存在已知短板（如全量驻留/硬编码/缺可观测性）。
优化题应在保持对外行为不变的前提下改进实现。"""
def process(record):
    rec = dict(record)
    rec.setdefault("_stage_version", "naive")
    return rec
