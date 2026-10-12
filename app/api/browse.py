"""Browse and search the command catalog."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, cast, String
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Command, Author, Review
from ..services import upstream

router = APIRouter()


@router.get("/v1/commands")
def list_commands(
    q: str | None = Query(None, description="Search query"),
    category: str | None = Query(None, description="Filter by category"),
    type: str | None = Query(None, description="Filter by package type: command, bundle, routine"),
    sort: str = Query("popular", description="Sort: popular, newest, name"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Browse/search published commands.

    With PANTRY_UPSTREAM_URL set, the upstream catalogue is merged in: this Pantry's own packages come first and win
    on a name clash. Every item carries ``"source"``: ``"local"`` (added by the administrator from a repository),
    ``"pantry"`` (published through this Pantry) or ``"upstream"`` (mirrored from the public Pantry).
    """
    if upstream.is_enabled():
        return _merged_listing(db, q, category, type, sort, page, per_page)

    query = db.query(Command).filter(Command.published.is_(True))

    # Package type filter
    if type:
        query = query.filter(Command.package_type == type)

    # Search
    if q:
        search = f"%{q}%"
        query = query.filter(
            or_(
                Command.command_name.ilike(search),
                Command.display_name.ilike(search),
                Command.description.ilike(search),
            )
        )

    # Category filter — cast to string and use LIKE for cross-DB compat
    if category:
        query = query.filter(cast(Command.categories, String).like(f'%"{category}"%'))

    # Sort
    if sort == "newest":
        query = query.order_by(Command.created_at.desc())
    elif sort == "name":
        query = query.order_by(Command.command_name.asc())
    else:  # popular
        query = query.order_by(Command.install_count.desc())

    # Pagination
    total = query.count()
    commands = query.offset((page - 1) * per_page).limit(per_page).all()

    return {
        "commands": [
            _summary(cmd)
            for cmd in commands
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def _summary(cmd: Command) -> dict:
    is_local = bool(cmd.author and cmd.author.is_local)
    return {
        "command_name": cmd.command_name,
        "display_name": cmd.display_name,
        "description": cmd.description,
        "author": ("local" if is_local else cmd.author.github_username) if cmd.author else None,
        "latest_version": cmd.latest_version,
        "categories": cmd.categories or [],
        "install_count": cmd.install_count,
        "danger_rating": cmd.danger_rating,
        "verified": cmd.verified,
        "icon_url": cmd.icon_url,
        "package_type": cmd.package_type or "command",
        "components": cmd.components or [],
        # "local": added by the administrator from a repository (private, unreviewed); "pantry": published through
        # this Pantry's own pipeline; "upstream": mirrored from the public Pantry.
        "source": "local" if is_local else "pantry",
        "created_at": cmd.created_at.isoformat() if cmd.created_at else None,
    }


def _merged_listing(db: Session, q, category, type_, sort, page, per_page) -> dict:
    local_all = [_summary(c) for c in db.query(Command).filter(Command.published.is_(True)).all()]
    local = upstream.filter_summaries(local_all, q, category, type_)
    local_names = {c["command_name"] for c in local_all}
    remote = [
        i for i in upstream.filter_summaries(upstream.list_commands(), q, category, type_)
        if i.get("command_name") not in local_names
    ]
    # Private packages first, then the public ones; each group ordered by the requested sort.
    def order(items: list[dict]) -> list[dict]:
        if sort == "name":
            return sorted(items, key=lambda i: str(i.get("command_name", "")).lower())
        if sort == "newest":
            return sorted(items, key=lambda i: str(i.get("created_at") or ""), reverse=True)
        return sorted(items, key=lambda i: i.get("install_count") or 0, reverse=True)

    merged = order(local) + order(remote)
    start = (page - 1) * per_page
    return {
        "commands": merged[start:start + per_page],
        "total": len(merged),
        "page": page,
        "per_page": per_page,
    }


@router.get("/v1/categories")
def list_categories(db: Session = Depends(get_db)):
    """List all categories with command counts."""
    commands = db.query(Command).filter(Command.published.is_(True)).all()

    category_counts: dict[str, int] = {}
    for cmd in commands:
        for cat in (cmd.categories or []):
            category_counts[cat] = category_counts.get(cat, 0) + 1
    if upstream.is_enabled():
        local_names = {c.command_name for c in commands}
        for item in upstream.list_commands():
            if item.get("command_name") in local_names:
                continue
            for cat in (item.get("categories") or []):
                category_counts[cat] = category_counts.get(cat, 0) + 1

    return {
        "categories": [
            {"name": name, "count": count}
            for name, count in sorted(category_counts.items(), key=lambda x: -x[1])
        ]
    }
