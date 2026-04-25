"""FastAPI application wiring.

Run with::

    uv run uvicorn api.app:app --host 127.0.0.1 --port 8000 --reload

or::

    uv run stakemate-api
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.routers import delegators, status, users, validators

app = FastAPI(
    title="StarknetStakeMate API",
    version="2.0.0",
    description="Service layer for the Telegram bot, the Mini App, and the local dashboard.",
)

# Telegram WebApp loads the page from a different origin than the API when you
# use the WebAppInfo(url=...) pointer. Allow it to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten once the Mini App has a fixed host
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(status.router)
app.include_router(validators.router)
app.include_router(delegators.router)
app.include_router(users.router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness probe — no RPC or DB hits, just proves the process is up."""
    return {"status": "ok"}

# Mount the Mini App bundle so `uvicorn` alone serves both halves in dev.
_WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
if _WEBAPP_DIR.is_dir():
    app.mount("/app", StaticFiles(directory=str(_WEBAPP_DIR), html=True), name="webapp")


@app.get("/", include_in_schema=False, response_model=None)
async def root():
    """Serve the Mini App's index at the bare ``/`` URL.

    We can't redirect to ``/app/`` here: Telegram WebApp passes ``initData``
    via the URL fragment (``#tgWebAppData=...``), and a 3xx response can
    drop / mangle the fragment in some Telegram clients — leaving the page
    without auth context and forcing a ``tg_id`` query fallback. The HTML
    references its assets (``style.css``, ``app.js``) by absolute paths
    rooted at ``/app/`` so they load fine regardless of which URL hits this.
    """
    index = _WEBAPP_DIR / "index.html"
    if index.is_file():
        return FileResponse(str(index))
    return {"name": "StarknetStakeMate API", "docs": "/docs", "app": "/app"}


def run() -> None:
    """Entry point used by ``uv run stakemate-api``."""
    import uvicorn

    uvicorn.run(
        "api.app:app",
        host=os.getenv("API_HOST", "127.0.0.1"),
        port=int(os.getenv("API_PORT", "8000")),
        reload=False,
    )
