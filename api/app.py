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


@app.on_event("startup")
async def _warm_contracts() -> None:
    """Pre-build cached Contract objects.

    starknet-py's ``Contract.from_address`` parses each ABI synchronously and
    blocks the event loop for ~30s on first call (huge handwritten Cairo
    interfaces). Without this, the very first Mini App request to /status
    hangs through the cold-start while Caddy's upstream timeout fires —
    users see a 30–70s spinner and then a connection drop.
    """
    import asyncio

    from utils.logger import logger

    async def _warm() -> None:
        try:
            # Off-load the blocking ABI parses; we're inside startup but the
            # event loop is already running, so pure sync calls would freeze
            # the whole API for the duration.
            from services.attestation_service import _attestation_contract
            from services.staking_service import _staking_contract, warm_pool_abi

            await asyncio.to_thread(_staking_contract)
            await asyncio.to_thread(_attestation_contract)
            await asyncio.to_thread(warm_pool_abi)
            logger.info("API: contract ABIs warmed up")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"API: contract warm-up skipped: {exc}")

    # Fire-and-forget — don't block the readiness check, but do start
    # warming immediately so the first user request likely lands warm.
    asyncio.create_task(_warm())

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
