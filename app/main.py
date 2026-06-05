"""Jarvis Pantry — Community command catalog API.

Cloud-hosted service for browsing, searching, and downloading
community-created voice commands for the Jarvis assistant.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import admin_bulk, browse, command_detail, download, forge, forge_drafts, manage, routines, submit, reviews
from .config import get_settings
from .services.callback_timeout_watcher import callback_timeout_watcher
from .services.job_queue import validation_queue

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown for the validation queue and background tasks."""
    import asyncio

    settings = get_settings()
    if settings.container_runner == "github_actions" and not settings.pantry_callback_signing_key:
        raise RuntimeError(
            "PANTRY_CALLBACK_SIGNING_KEY is required when PANTRY_CONTAINER_RUNNER=github_actions. "
            "Set it to a 32+ byte secret shared with the jarvis-pantry-runner GHA environment.",
        )
    await validation_queue.start(num_workers=settings.max_concurrent_container_tests)

    # Sweep stale temp dirs from crashed runs
    _cleanup_stale_repos()

    # Background task: clean expired forge drafts every 5 minutes
    draft_cleanup_task = asyncio.create_task(_draft_cleanup_loop())

    # Background task: retry stalled awaiting_container dispatches (#22)
    callback_timeout_task = asyncio.create_task(callback_timeout_watcher())

    yield

    callback_timeout_task.cancel()
    draft_cleanup_task.cancel()
    await validation_queue.stop()


def _cleanup_stale_repos() -> None:
    """Remove /tmp/jarvis-store-* dirs older than 1 hour."""
    import os

    cutoff = time.time() - 3600
    for path in glob.glob("/tmp/jarvis-store-*"):
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                logger.info("Cleaned up stale repo dir: %s", path)
        except OSError:
            pass


async def _draft_cleanup_loop() -> None:
    """Delete expired forge drafts every 5 minutes."""
    import asyncio

    from .api.forge_drafts import cleanup_expired_drafts
    from .db import SessionLocal

    while True:
        await asyncio.sleep(300)
        try:
            db = SessionLocal()
            try:
                count = cleanup_expired_drafts(db)
                if count:
                    logger.info("Cleaned %d expired forge drafts", count)
            finally:
                db.close()
        except Exception as e:
            logger.warning("Draft cleanup error: %s", e)


app = FastAPI(
    title="Jarvis Pantry",
    version="0.1.0",
    description="Community command store for Jarvis voice assistant",
    lifespan=lifespan,
)

# CORS — allow any origin for public catalog browsing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Register routers
app.include_router(browse.router, tags=["catalog"])
app.include_router(command_detail.router, tags=["catalog"])
app.include_router(download.router, tags=["download"])
app.include_router(submit.router, tags=["submission"])
app.include_router(reviews.router, tags=["reviews"])
app.include_router(manage.router, tags=["auth", "management"])
app.include_router(forge.router, tags=["forge"])
app.include_router(forge_drafts.router, tags=["forge"])
app.include_router(routines.router, tags=["routines"])
app.include_router(admin_bulk.router, tags=["admin", "bulk"])


# Operator UI — single-page static asset for bulk uploads. Off by default
# so self-hosted pantry installs don't expose an unfamiliar admin surface;
# the operator running the central pantry sets PANTRY_OPERATOR_UI=1 to
# mount it. The endpoints behind the page (admin_bulk router) are always
# present but gated by ADMIN_API_KEY — same posture as the existing
# /v1/admin/commands/* endpoints.
if os.getenv("PANTRY_OPERATOR_UI") == "1":
    from pathlib import Path as _Path
    from fastapi.staticfiles import StaticFiles
    _operator_dir = _Path(__file__).parent / "static" / "operator"
    if _operator_dir.is_dir():
        app.mount(
            "/operator",
            StaticFiles(directory=str(_operator_dir), html=True),
            name="operator",
        )
        logger.info("Mounted operator UI at /operator")
    else:
        logger.warning(
            "PANTRY_OPERATOR_UI=1 set but %s not found — operator UI not mounted",
            _operator_dir,
        )


@app.get("/health")
def health():
    return {"status": "ok", "service": "jarvis-pantry"}
