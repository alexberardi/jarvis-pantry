"""Shared check for endpoints that only an administrator may call."""

from __future__ import annotations

import hmac

from fastapi import HTTPException


def require_admin_key(provided: str | None, purpose: str) -> None:
    """Refuse unless ``ADMIN_API_KEY`` is configured and ``provided`` matches it.

    These endpoints change what nodes install or where the Pantry sends requests, so they never run on an open
    Pantry: with no key configured they answer 403, with a wrong or missing key 401. The comparison is constant time.
    """
    from ..config import get_settings

    expected = getattr(get_settings(), "admin_api_key", "") or ""
    if not expected:
        raise HTTPException(403, f"{purpose} needs ADMIN_API_KEY to be set on this Pantry")
    if not hmac.compare_digest((provided or "").encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "Admin key required")
