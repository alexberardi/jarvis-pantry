# jarvis-pantry

Community command store for Jarvis voice assistant — HACS-style Pantry.

## Quick Reference

```bash
# Setup
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # Edit DATABASE_URL

# Run (port 7720)
./run.sh                    # Local dev
./run.sh --docker           # Docker dev (uses shared jarvis-postgres)
./run.sh --docker --build   # Docker dev with rebuild

# Production
./run-prod.sh [--build]

# Test
.venv/bin/pytest tests/ -v
```

## Architecture

Cloud-hosted FastAPI service following the `jarvis-notifications` pattern.
Provides browse/search/download API for community commands.

```
app/
├── main.py                    # FastAPI app, CORS, routers
├── config.py                  # Pydantic Settings
├── db.py                      # SQLAlchemy engine + session
├── models.py                  # Author, Command, Version, Review, etc.
├── auth.py                    # GitHub OAuth + Household JWT
├── rate_limiter.py            # In-memory rate limiting
├── api/
│   ├── browse.py              # GET /v1/commands, /v1/categories
│   ├── command_detail.py      # GET /v1/commands/{name}
│   ├── download.py            # GET /v1/commands/{name}/download
│   ├── submit.py              # POST /v1/commands (submission + AI review)
│   └── reviews.py             # GET/POST reviews, admin verify/unpublish
└── services/
    ├── github_service.py      # Clone, validate structure, parse manifest
    ├── security_review.py     # AI review using submitter's API key (BYOK)
    └── submission_pipeline.py # Orchestrates: clone -> validate -> AI review -> publish
```

## Auth

- **GitHub OAuth** — For authors (submissions, reviews)
- **Household JWT** — For node installations (downloads, install tracking)
- **Admin key** — For verification/moderation (`X-Admin-Key` header)

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | — | Health check |
| GET | `/v1/commands` | — | Browse/search |
| GET | `/v1/commands/{name}` | — | Detail + versions + reviews |
| GET | `/v1/commands/{name}/versions` | — | Version history |
| GET | `/v1/commands/{name}/download` | JWT | Clone URL + tag |
| POST | `/v1/commands` | GitHub | Submit command (with BYOK AI review) |
| GET | `/v1/submissions/{id}` | GitHub | Check submission status |
| GET | `/v1/commands/{name}/reviews` | — | List reviews |
| POST | `/v1/commands/{name}/reviews` | GitHub | Submit/update review |
| POST | `/v1/commands/{name}/installed` | JWT | Install counter |
| GET | `/v1/categories` | — | Category list |
| POST | `/v1/admin/commands/{name}/verify` | Admin | Mark verified |
| POST | `/v1/admin/commands/{name}/unpublish` | Admin | Unpublish |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | - | PostgreSQL connection |
| `PANTRY_PORT` | 7720 | API port |
| `GITHUB_CLIENT_ID` | - | GitHub OAuth client ID |
| `GITHUB_CLIENT_SECRET` | - | GitHub OAuth client secret |
| `STORE_JWT_SECRET` | - | Shared secret for household JWT |
| `ADMIN_API_KEY` | - | Admin endpoint protection |
| `ALERT_WEBHOOK_URL` | - | Optional abuse alerting |

## Database

PostgreSQL required. Six tables: `authors`, `commands`, `command_versions`, `security_reports`, `reviews`, `submissions`.

Run migrations: `alembic upgrade head`

## Dependencies

**Service Dependencies:**
- **Required**: PostgreSQL — Data storage
- **Optional**: GitHub API — OAuth and repo validation

**Used By:**
- `jarvis-node-setup` — Browse/install/update commands via CLI
- `jarvis-node-mobile` — (future) Store UI screen
- `jarvis-admin` — (future) Store management tab

## Port: 7720
