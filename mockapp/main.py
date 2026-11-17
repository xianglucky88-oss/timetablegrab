"""离线 mock server（fastapi）：回放 fixtures，供抓取/RPA 类题目在无公网联调。
基线 happy-path 不依赖此服务（直接读 fixtures）；保留为后续功能开发靶。"""
import json
from pathlib import Path
try:
    from fastapi import FastAPI
except Exception:  # pragma: no cover
    FastAPI = None

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "input.json"

if FastAPI is not None:
    app = FastAPI(title="mock")

    @app.get("/records")
    def records():
        return json.loads(FIX.read_text(encoding="utf-8"))
