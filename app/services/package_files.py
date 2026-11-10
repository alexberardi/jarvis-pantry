"""Read-only look into published packages for the Forge's model: search the catalogue, list a package's files at
its latest published version and read one of them.

Files come from GitHub, and only for packages this Pantry lists (its own, or the upstream mirror's): the repository
and ref come from the catalogue, never from the caller, and every request goes to GitHub's own hosts. Listings and
files are cached in memory; a ref is the reviewed commit SHA (or the version's tag), so the content rarely changes.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from ..models import Command, CommandVersion
from . import upstream
from .github_service import RepoValidationError, parse_repo_owner_name

MAX_FILE_BYTES = 200 * 1024
MAX_LISTING = 500
_TTL = 600.0
_TIMEOUT = 10.0
_MAX_CACHE = 256

_cache: dict[tuple, tuple[float, Any]] = {}

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9_./-]+$")


class PackageFilesError(RuntimeError):
    """GitHub could not be reached or answered with an error (a 502 to the caller)."""


class PackageNotFound(LookupError):
    """No such published package, version or file (a 404 to the caller)."""


def clear_cache() -> None:
    _cache.clear()


def _cached(key: tuple, load):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    value = load()
    if len(_cache) >= _MAX_CACHE:
        _cache.clear()  # shortcut: drop everything when full; an LRU only if hit rates ever matter
    _cache[key] = (now, value)
    return value


def _matches_type(item: dict[str, Any], component_type: str) -> bool:
    if component_type == "bundle":
        return item.get("package_type") == "bundle"
    types = {c.get("type") for c in item.get("components") or [] if isinstance(c, dict)}
    # A single command often declares no components.
    return component_type in types or (component_type == "command" and not types and item.get("package_type") != "bundle")


def search(db: Session, q: str, component_type: str | None, limit: int = 20) -> list[dict[str, Any]]:
    from ..api.browse import _summary

    items = [_summary(c) for c in db.query(Command).filter(Command.published.is_(True)).all()]
    if upstream.is_enabled():
        names = {i["command_name"] for i in items}
        items += [i for i in upstream.list_commands() if i.get("command_name") not in names]
    items = upstream.filter_summaries(items, q.strip() or None, None, None)
    if component_type:
        items = [i for i in items if _matches_type(i, component_type)]
    items.sort(key=lambda i: i.get("install_count") or 0, reverse=True)
    return [
        {
            "name": i.get("command_name"),
            "display_name": i.get("display_name"),
            "description": i.get("description"),
            "package_type": i.get("package_type") or "command",
            "components": [
                {"type": c.get("type"), "name": c.get("name"), "path": c.get("path")}
                for c in i.get("components") or [] if isinstance(c, dict)
            ],
            "latest_version": i.get("latest_version"),
        }
        for i in items[:limit]
    ]


def _locate(db: Session, name: str) -> tuple[str, str, str]:
    """(owner, repo, ref) of a published package's latest version, from the catalogue only."""
    cmd = db.query(Command).filter(Command.command_name == name, Command.published.is_(True)).first()
    if cmd:
        repo_url = cmd.github_repo_url or ""
        ver = db.query(CommandVersion).filter(CommandVersion.command_id == cmd.id).order_by(
            CommandVersion.published_at.desc()).first()
        ref = (ver.git_commit_sha or ver.git_tag) if ver else None
    elif upstream.is_enabled() and (detail := upstream.get_detail(name)) is not None:
        repo_url = str(detail.get("github_repo_url") or "")
        versions = (upstream.get_detail(name, "/versions") or {}).get("versions") or []
        ref = versions[0].get("git_tag") if versions and isinstance(versions[0], dict) else None
    else:
        raise PackageNotFound(f"Package '{name}' not found")
    if not ref:
        raise PackageNotFound(f"Package '{name}' has no published version tag")
    try:
        owner, repo = parse_repo_owner_name(repo_url)
    except RepoValidationError:
        raise PackageNotFound(f"Package '{name}' has no GitHub repository")
    # The URL and ref may come from the upstream mirror: keep them to plain names so they stay inside the path.
    if not (_SAFE_SEGMENT.match(owner) and _SAFE_SEGMENT.match(repo) and _SAFE_REF.match(ref)) or ".." in ref:
        raise PackageNotFound(f"Package '{name}' has no usable GitHub repository")
    return owner, repo, ref


def _headers() -> dict[str, str]:
    from ..config import get_settings
    token = (getattr(get_settings(), "pantry_github_token", "") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _get(url: str, accept: str, max_bytes: int) -> bytes:
    try:
        with httpx.stream("GET", url, headers={"Accept": accept, **_headers()}, timeout=_TIMEOUT,
                          follow_redirects=False) as resp:
            if resp.status_code == 404:
                raise PackageNotFound("Not found on GitHub")
            if resp.status_code != 200:
                raise PackageFilesError(f"GitHub answered HTTP {resp.status_code}")
            data = bytearray()
            for chunk in resp.iter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise PackageFilesError("GitHub's reply was larger than the allowed size")
            return bytes(data)
    except httpx.HTTPError as e:
        raise PackageFilesError(f"Could not reach GitHub: {type(e).__name__}") from e


def _tree(owner: str, repo: str, ref: str) -> dict[str, Any]:
    def load():
        import json
        body = json.loads(_get(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees/{quote(ref, safe='')}?recursive=1",
            "application/vnd.github+json", 10 * 1024 * 1024,
        ))
        blobs = [{"path": e["path"], "size": e.get("size", 0)} for e in body.get("tree") or [] if e.get("type") == "blob"]
        return {"files": blobs[:MAX_LISTING], "truncated": bool(body.get("truncated")) or len(blobs) > MAX_LISTING}
    return _cached(("tree", owner, repo, ref), load)


def _tree_at(owner: str, repo: str, ref: str) -> tuple[str, dict[str, Any]]:
    """The listing at the published ref, or at the default branch (HEAD) when GitHub has no such
    ref: a published tag can be missing from the repo (zwave's v1.0.2), and a reference read
    doesn't need the exact version. Returns the ref actually read."""
    try:
        return ref, _tree(owner, repo, ref)
    except PackageNotFound:
        return "HEAD", _tree(owner, repo, "HEAD")


def list_files(db: Session, name: str) -> dict[str, Any]:
    owner, repo, ref = _locate(db, name)
    ref, tree = _tree_at(owner, repo, ref)
    return {"package": name, "ref": ref, **tree}


def _check_path(path: str) -> str:
    path = path.strip()
    parts = path.split("/")
    if not path or path.startswith("/") or "\\" in path or any(p in ("", ".", "..") for p in parts):
        raise ValueError("Path must be relative to the package root, with no '..' or empty parts")
    return path


def read_file(db: Session, name: str, path: str) -> dict[str, Any]:
    path = _check_path(path)
    owner, repo, ref = _locate(db, name)
    ref, tree = _tree_at(owner, repo, ref)
    entry = next((f for f in tree["files"] if f["path"] == path), None)
    if entry is None:
        raise PackageNotFound(f"No file '{path}' in {name}")
    if entry["size"] > MAX_FILE_BYTES:
        raise ValueError(f"'{path}' is {entry['size']} bytes; the limit is {MAX_FILE_BYTES}")

    def load():
        raw = _get(
            f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(ref, safe='')}/{quote(path)}",
            "application/octet-stream", MAX_FILE_BYTES,
        )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"'{path}' is not a text file")
    return {"package": name, "ref": ref, "path": path, "content": _cached(("file", owner, repo, ref, path), load)}
