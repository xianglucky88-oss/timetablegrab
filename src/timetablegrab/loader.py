"""单一职责：从本地 fixtures 读取原始记录（happy-path 摄取入口，不联网）。"""
import json
from .config import FIXTURES

def load():
    with open(FIXTURES, encoding="utf-8") as f:
        return json.load(f)
