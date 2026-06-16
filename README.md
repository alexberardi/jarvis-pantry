# jarvis-pantry

The community command store ("Pantry") and AI **Forge** for the Jarvis voice assistant. It's a public FastAPI service where authors can publish, browse, review, and download Jarvis commands and routines — and where new commands can be generated and validated through an automated pipeline.

Runs on **port 7721**.

## What it does

- **Catalog** — public, browsable store of Jarvis commands (`/` catalog + command detail, search/browse).
- **Downloads** — serve installable command packages to nodes/clients.
- **Submissions** — GitHub-OAuth-authenticated authors submit and manage their commands.
- **Reviews** — community reviews/ratings on commands.
- **Routines** — shareable routines store.
- **Forge** — AI-assisted command generation plus a validation pipeline: LLM generation, static analysis, security review, lockfile resolution, and sandboxed **container tests** before a submission is accepted (see `app/services/`).
- **Admin** — bulk management endpoints protected by `ADMIN_API_KEY`.

## Requirements

- Python 3.11+
- Docker & Docker Compose (recommended)
- PostgreSQL (`DATABASE_URL`)

## Setup & run

```bash
cp .env.example .env   # then fill in values

# Local dev (hot reload):
./run.sh

# Docker dev:
./run.sh --docker          # add --build, or --rebuild for a clean build

# Production:
./run-prod.sh
```

The app module is `app.main:app`. The service listens on `${PANTRY_PORT}` (defaults to **7721**; the Docker image and `.env.example` set 7721). Alembic migrations (`alembic upgrade head`) run automatically on container startup.

- Swagger UI: http://localhost:7721/docs

It also deploys to Fly.io (`fly.toml`, `Dockerfile.fly`).

## Environment

Copy `.env.example` to `.env`. Key variables:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth for author submissions/management |
| `ADMIN_API_KEY` | Protects admin endpoints |
| `PANTRY_PORT` | Port to serve on (default 7721) |
| `ALERT_WEBHOOK_URL` | Optional Slack/Discord webhook for abuse alerting |
| `BYPASS_LLM_KEY` | Dev: skip the AI-review key requirement |
| `JARVIS_SDK_PATH` | Path to `jarvis-command-sdk` used by container tests |
| `CONTAINER_TEST_TIMEOUT` / `MAX_CONCURRENT_CONTAINER_TESTS` / `MAX_CONCURRENT_CLONES` | Forge validation pipeline tuning |
| `SUBMISSION_RATE_LIMIT_PER_HOUR` / `RATE_LIMIT_DISABLED` | Per-IP submission rate limiting |

## Testing

```bash
pytest
```

## License

AGPL-3.0 (see `LICENSE`).
