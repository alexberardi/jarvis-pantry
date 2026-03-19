"""Jarvis Pantry — Community command catalog API.

Cloud-hosted service for browsing, searching, and downloading
community-created voice commands for the Jarvis assistant.
"""

from __future__ import annotations

import glob
import logging
import shutil
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import browse, command_detail, download, forge, manage, submit, reviews
from .config import get_settings
from .services.job_queue import validation_queue

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown for the validation queue."""
    settings = get_settings()
    await validation_queue.start(num_workers=settings.max_concurrent_container_tests)

    # Sweep stale temp dirs from crashed runs
    _cleanup_stale_repos()

    yield

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
    allow_methods=["GET", "POST"],
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


@app.get("/health")
def health():
    return {"status": "ok", "service": "jarvis-pantry"}
