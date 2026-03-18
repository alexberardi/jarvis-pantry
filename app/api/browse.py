"""Browse and search the command catalog."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, cast, String
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Command, Author, Review

router = APIRouter()


@router.get("/v1/commands")
def list_commands(
    q: str | None = Query(None, description="Search query"),
    category: str | None = Query(None, description="Filter by category"),
    sort: str = Query("popular", description="Sort: popular, newest, name"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Browse/search published commands."""
    query = db.query(Command).filter(Command.published.is_(True))

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
            {
                "command_name": cmd.command_name,
                "display_name": cmd.display_name,
                "description": cmd.description,
                "author": cmd.author.github_username if cmd.author else None,
                "latest_version": cmd.latest_version,
                "categories": cmd.categories or [],
                "install_count": cmd.install_count,
                "danger_rating": cmd.danger_rating,
                "verified": cmd.verified,
                "icon_url": cmd.icon_url,
                "package_type": cmd.package_type or "command",
                "components": cmd.components or [],
            }
            for cmd in commands
        ],
        "total": total,
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

    return {
        "categories": [
            {"name": name, "count": count}
            for name, count in sorted(category_counts.items(), key=lambda x: -x[1])
        ]
    }
