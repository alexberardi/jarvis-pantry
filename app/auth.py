"""Authentication for the command store.

GitHub OAuth for authors (submissions, reviews, package management).
Public catalog endpoints (browse, detail, download) require no auth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .config import get_settings

if TYPE_CHECKING:
    from .models import Author
from .db import get_db


async def exchange_github_code(
    code: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Exchange a GitHub OAuth code for an access token and user info.

    Defaults to the regular submission-flow credentials when client_id /
    client_secret aren't passed — the operator bulk page hands in its own
    pair (loaded from PANTRY_OPERATOR_GITHUB_CLIENT_*).

    Returns:
        Dict with keys: access_token, github_id, github_username, display_name, avatar_url
    """
    settings = get_settings()
    client_id = client_id or settings.github_client_id
    client_secret = client_secret or settings.github_client_secret
    if not client_id or not client_secret:
        raise HTTPException(503, "GitHub OAuth not configured")

    async with httpx.AsyncClient() as client:
        # Exchange code for token
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": client_id,
                "client_secret": client_secret,
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


# Usernames of Author rows that stand for the service itself, not a GitHub
# account. A login never takes one over by name: GitHub accounts with these
# names can exist or be registered by anyone.
RESERVED_USERNAMES = frozenset({"pantry-operator", "local"})


def resolve_author(db: Session, user_data: dict) -> "Author":
    """The Author for a verified GitHub user (``GET /user``), created if new.

    Matched by GitHub id. A name match only claims a row created before
    OAuth (``github_id == 0``) and never a reserved one; any other row
    holding the name belongs to a different GitHub account (one that was
    renamed, or a service row), so this user gets a distinct username
    instead of that row.
    """
    from .models import Author

    github_id = user_data["id"]
    login = user_data["login"]
    display_name = user_data.get("name") or login
    avatar_url = user_data.get("avatar_url")

    def username_for(row_id: int | None) -> str:
        holder = db.query(Author).filter(Author.github_username == login).first()
        if holder is None or holder.id == row_id:
            return login
        return f"{login}~{github_id}"

    author = db.query(Author).filter(Author.github_id == github_id).first()
    if author:
        if author.github_username != login:
            # Renamed on GitHub.
            author.github_username = username_for(author.id)
            author.display_name = display_name
            author.avatar_url = avatar_url
            db.commit()
        return author

    pre_oauth = db.query(Author).filter(
        Author.github_username == login, Author.github_id == 0,
        Author.is_local.is_(False),  # defence in depth: the local author is not a GitHub account
    ).first()
    if pre_oauth and login.casefold() not in RESERVED_USERNAMES:
        pre_oauth.github_id = github_id
        pre_oauth.display_name = display_name
        pre_oauth.avatar_url = avatar_url
        db.commit()
        return pre_oauth

    author = Author(
        github_id=github_id,
        github_username=username_for(None),
        display_name=display_name,
        avatar_url=avatar_url,
    )
    db.add(author)
    db.commit()
    db.refresh(author)
    return author


def validate_github_token(
    authorization: str = Header(..., alias="Authorization"),
    db: Session = Depends(get_db),
) -> "Author":
    """Validate a GitHub access token and return the Author record.

    Creates the Author if this is their first interaction.
    """
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

    author = resolve_author(db, resp.json())

    if author.suspended:
        raise HTTPException(403, "Author account is suspended")

    return author
