"""离线 mock server（fastapi）：回放 fixtures，供本地联调。"""
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
