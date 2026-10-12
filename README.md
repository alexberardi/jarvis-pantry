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

## Run it locally with your own LLM server

The Forge can build packages with a model on your own hardware instead of a paid API.

```bash
cp .env.local.example .env.local            # set PANTRY_DB_PASSWORD (and PANTRY_PUBLIC_API_URL on a server)
docker compose -f docker-compose.local.yaml --env-file .env.local up -d --build
```

The local LLM features are behind one switch, `FORGE_LOCAL_LLM_ENABLED` (`true` in the local compose file, **off by
default** elsewhere). While it is off the Settings link is hidden, `/v1/forge/local-llm*` return 404, local models are not
listed and the Pantry behaves exactly as upstream; `GET /v1/forge/features` reports the state.

Open <http://localhost:7720>, go to **Settings**, enter the address of any OpenAI-compatible server (llama-swap,
llama.cpp `llama-server`, vLLM, Ollama) and its API key if it has one, and press **Detect models**. The models it
reports (`GET /v1/models`) fill the Forge's model list; pick a default and save. Settings are stored in the database.

Also available over the API: `GET/PUT /v1/forge/local-llm`, `POST /v1/forge/local-llm/detect`, and `GET /v1/forge/models`
(local models first, marked `"default": true`). Set `ADMIN_API_KEY` to require `X-Admin-Key` for changes and probes; with
none set, anyone who can reach the port can change them, so keep it on a trusted network. The server fetches whatever
address is saved, so do not expose the API to untrusted clients.

The compose file mounts no Docker socket, so the sandboxed container tests used when publishing a submission are
unavailable. Local models are slower and weaker than hosted ones; the Forge's static checks do not import or run the
generated code, so test what it produces.

## Public and private packages together (upstream mirror)

Set `PANTRY_UPSTREAM_URL` (the local compose file defaults it to the public Pantry's API) and this Pantry lists the public
catalogue **and** its own packages as one store, so nodes need only this address:

- `GET /v1/commands` merges both (your packages first, then public ones; a local name shadows a public one), each item with
  `"source": "local" | "upstream"`. Search, category and type filters, sorting and pagination apply to the merged list, and
  `/v1/categories` counts both.
- `GET /v1/commands/{name}`, `/versions` and `/download` answer locally first and fall back to the public Pantry. Nodes still
  clone each package from its GitHub repository.
- The public list is cached for `PANTRY_UPSTREAM_TTL_SECONDS` (300). If the upstream is offline the last copy is served, or
  just your own packages.

Point a node at it with `command_store_url` in its config (for example `http://pantry.lan:7721`).

**Adding a private package.** The public submission flow needs GitHub login and container tests, so for your own
repositories there is an administrator shortcut:

```bash
curl -s -X POST http://pantry.lan:7721/v1/admin/commands/from-repo \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"github_repo_url": "https://github.com/you/jarvis-cmd-thing", "ref": "v1.0.0"}'
```

It clones the repository, validates the manifest and publishes it as a local package. There is **no AI security review and
no container test**, and it refuses to run unless `ADMIN_API_KEY` is set, because nodes install what you add. A private
GitHub repository needs a token, in the request or in `PANTRY_GITHUB_TOKEN` (used for the clone only, never stored or
returned); a node installing a private repository needs its own access to it. `DELETE
/v1/admin/commands/{name}/local` unpublishes one. Re-run the POST to publish a new version.

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
