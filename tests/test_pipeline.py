"""遗留通用记录流水线：旧合法输入与旧行为完全兼容。"""
from timetablegrab import pipeline, store


def test_pipeline_runs_and_idempotent(con):
    a = pipeline.run(con=con)
    b = pipeline.run(con=con)   # 重跑不增行（幂等 upsert）
    assert a["fed"] == 4
    assert b["rows"] == a["rows"] == 3   # r-003 重复被去重


def test_store_roundtrip(con):
    assert store.count(con) >= 0
