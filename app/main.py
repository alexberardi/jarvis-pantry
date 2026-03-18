"""Jarvis Pantry — Community command catalog API.

Cloud-hosted service for browsing, searching, and downloading
community-created voice commands for the Jarvis assistant.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import browse, command_detail, download, submit, reviews
from .config import get_settings

app = FastAPI(
    title="Jarvis Pantry",
    version="0.1.0",
    description="Community command store for Jarvis voice assistant",
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


@app.get("/health")
def health():
    return {"status": "ok", "service": "jarvis-pantry"}
