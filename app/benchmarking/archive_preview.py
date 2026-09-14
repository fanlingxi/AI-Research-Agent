"""Read-only API over a disposable archive copy; no Worker or model calls."""

import os
from pathlib import Path

from fastapi.responses import JSONResponse


def create():
    path = Path(os.environ["KNOWLEDGE_DB_PATH"]).resolve(strict=True)
    root = Path(__file__).resolve().parents[2] / "data/runtime/resume-preview"
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Preview requires an isolated runtime database copy")
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["QDRANT_URL"] = "http://127.0.0.1:9"
    os.environ["NEO4J_URI"] = "bolt://127.0.0.1:9"
    from app.api.main import create_app
    from app.config.settings import get_settings

    get_settings.cache_clear()
    app = create_app()

    @app.middleware("http")
    async def read_only(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse(
                {"detail": "归档预览为只读，请在日常工作台创建新运行。"}, status_code=405
            )
        response = await call_next(request)
        response.headers["X-Archive-Preview"] = "read-only"
        return response

    return app
