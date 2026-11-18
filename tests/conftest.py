"""pytest 夹具：把所有运行时路径隔离到临时目录，测试之间互不污染。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from timetablegrab import config, store  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    for attr in ("DATA_DIR", "CACHE_DIR", "QUARANTINE_DIR", "REPORT_DIR"):
        monkeypatch.setattr(config, attr, tmp_path / attr.lower())
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    return tmp_path


@pytest.fixture()
def con(sandbox):
    c = store.connect()
    yield c
    c.close()
