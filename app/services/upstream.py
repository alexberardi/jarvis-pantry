"""Mirror of a public Pantry catalogue (``PANTRY_UPSTREAM_URL``) served next to this Pantry's own packages.

Local packages are the private ones. The upstream list is fetched whole (it is small), cached for
``PANTRY_UPSTREAM_TTL_SECONDS`` and filtered here, so search, category and type filters behave the same for both.
When the upstream is unreachable the last good copy is served, and an empty list if there never was one, so a slow
or offline upstream never takes the local catalogue down. Nodes still install from each package's GitHub repository;
the Pantry only brokers the lookup.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_MAX_PAGES = 20

_list_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_detail_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}


class UpstreamError(RuntimeError):
    """The upstream could not be reached or answered with an error."""


def base_url() -> str:
    from ..config import get_settings
    return (getattr(get_settings(), "pantry_upstream_url", "") or "").strip().rstrip("/")


def is_enabled() -> bool:
    return bool(base_url())


_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def _ttl() -> float:
    from ..config import get_settings
    return float(getattr(get_settings(), "pantry_upstream_ttl_seconds", 300))


def _timeout() -> float:
    from ..config import get_settings
    return float(getattr(get_settings(), "pantry_upstream_timeout_seconds", 8.0))


def clear_cache() -> None:
    _list_cache.clear()
    _detail_cache.clear()


def _get_json(path: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
    url = f"{base_url()}{path}"
    # Redirects are not followed (the configured URL is the one the administrator trusts) and the reply is capped,
    # so a hostile or misconfigured upstream cannot redirect the Pantry elsewhere or exhaust its memory.
    try:
        with httpx.stream("GET", url, params=params, timeout=_timeout(), follow_redirects=False) as resp:
            status = resp.status_code
            data = bytearray()
            for chunk in resp.iter_bytes():
                data.extend(chunk)
                if len(data) > _MAX_RESPONSE_BYTES:
                    raise UpstreamError("The upstream Pantry's reply was larger than the allowed size")
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach the upstream Pantry: {e}") from e
    try:
        body = json.loads(bytes(data))
    except ValueError:
        body = None
    return status, body


def list_commands() -> list[dict[str, Any]]:
    """Every upstream package summary, each marked ``"source": "upstream"``. Cached; stale on failure."""
    key = base_url()
    now = time.monotonic()
    hit = _list_cache.get(key)
    if hit and now - hit[0] < _ttl():
        return hit[1]
    try:
        items: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            status, body = _get_json("/v1/commands", {"page": page, "per_page": _PAGE_SIZE})
            if status != 200 or not isinstance(body, dict):
                raise UpstreamError(f"Upstream answered HTTP {status} for the package list")
            batch = body.get("commands") or []
            items.extend(batch)
            if not batch or len(items) >= int(body.get("total", len(items))):
                break
        for item in items:
            item["source"] = "upstream"
        _list_cache[key] = (now, items)
        return items
    except UpstreamError as e:
        logger.warning("Upstream catalogue unavailable: %s", e)
        return hit[1] if hit else []


def get_detail(command_name: str, suffix: str = "") -> dict[str, Any] | None:
    """Upstream detail (or ``suffix`` such as ``/versions``) for one package, None when it does not exist there."""
    key = (base_url(), f"{command_name}{suffix}")
    now = time.monotonic()
    hit = _detail_cache.get(key)
    if hit and now - hit[0] < _ttl():
        return hit[1]
    try:
        status, body = _get_json(f"/v1/commands/{quote(command_name, safe='')}{suffix}")
    except UpstreamError as e:
        logger.warning("Upstream lookup of %s failed: %s", command_name, e)
        return hit[1] if hit else None
    if status == 404:
        _detail_cache[key] = (now, None)
        return None
    if status != 200 or not isinstance(body, (dict, list)):
        logger.warning("Upstream answered HTTP %s for %s", status, command_name)
        return hit[1] if hit else None
    if isinstance(body, dict):
        body["source"] = "upstream"
    _detail_cache[key] = (now, body)  # type: ignore[assignment]
    return body  # type: ignore[return-value]


def get_download(command_name: str, version: str | None) -> tuple[int, Any]:
    """Pass a download lookup through to the upstream. Not cached: it counts an install there."""
    params = {"version": version} if version else None
    return _get_json(f"/v1/commands/{quote(command_name, safe='')}/download", params)


def filter_summaries(
    items: list[dict[str, Any]], q: str | None, category: str | None, package_type: str | None,
) -> list[dict[str, Any]]:
    out = items
    if package_type:
        out = [i for i in out if (i.get("package_type") or "command") == package_type]
    if category:
        out = [i for i in out if category in (i.get("categories") or [])]
    if q:
        needle = q.lower()
        out = [
            i for i in out
            if needle in str(i.get("command_name", "")).lower()
            or needle in str(i.get("display_name", "")).lower()
            or needle in str(i.get("description", "")).lower()
        ]
    return out
