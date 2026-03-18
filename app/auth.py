"""Authentication for the command store.

Two auth modes:
1. GitHub OAuth - for authors (submissions, reviews)
2. Household JWT - for node installations (downloads, install tracking)
"""

from __future__ import annotations

from typing import Any

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db


def validate_household_jwt(
    authorization: str = Header(..., alias="Authorization"),
    x_household_id: str = Header(..., alias="X-Household-Id"),
) -> str:
    """Validate household JWT and return household_id.

    Same pattern as jarvis-notifications-relay.
    """
    settings = get_settings()
    if not settings.store_jwt_secret:
        raise HTTPException(503, "Store JWT not configured")

    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header")

    token = authorization[7:]
    try:
        payload = jwt.decode(token, settings.store_jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

    jwt_household = payload.get("household_id")
    if jwt_household != x_household_id:
        raise HTTPException(403, "Household ID mismatch")

    return jwt_household


async def exchange_github_code(code: str) -> dict[str, Any]:
    """Exchange a GitHub OAuth code for an access token and user info.

    Returns:
        Dict with keys: access_token, github_id, github_username, display_name, avatar_url
    """
    settings = get_settings()
    if not settings.github_client_id or not settings.github_client_secret:
        raise HTTPException(503, "GitHub OAuth not configured")

    async with httpx.AsyncClient() as client:
        # Exchange code for token
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
            timeout=15.0,
        )
        if token_resp.status_code != 200:
            raise HTTPException(401, "GitHub token exchange failed")

        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            error = token_data.get("error_description", "Unknown error")
            raise HTTPException(401, f"GitHub OAuth failed: {error}")

        # Get user info
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15.0,
        )
        if user_resp.status_code != 200:
            raise HTTPException(401, "Failed to fetch GitHub user info")

        user_data = user_resp.json()

    return {
        "access_token": access_token,
        "github_id": user_data["id"],
        "github_username": user_data["login"],
        "display_name": user_data.get("name") or user_data["login"],
        "avatar_url": user_data.get("avatar_url"),
    }


def validate_github_token(
    authorization: str = Header(..., alias="Authorization"),
    db: Session = Depends(get_db),
) -> "Author":
    """Validate a GitHub access token and return the Author record.

    Creates the Author if this is their first interaction.
    """
    from .models import Author

    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header")

    token = authorization[7:]

    # Verify token with GitHub
    with httpx.Client() as client:
        resp = client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15.0,
        )

    if resp.status_code != 200:
        raise HTTPException(401, "Invalid GitHub token")

    user_data = resp.json()
    github_id = user_data["id"]
    github_username = user_data["login"]

    # Find or create author
    author = db.query(Author).filter(Author.github_id == github_id).first()
    if not author:
        author = Author(
            github_id=github_id,
            github_username=github_username,
            display_name=user_data.get("name") or github_username,
            avatar_url=user_data.get("avatar_url"),
        )
        db.add(author)
        db.commit()
        db.refresh(author)
    elif author.github_username != github_username:
        # Username may have changed
        author.github_username = github_username
        author.display_name = user_data.get("name") or github_username
        author.avatar_url = user_data.get("avatar_url")
        db.commit()

    if author.suspended:
        raise HTTPException(403, "Author account is suspended")

    return author
