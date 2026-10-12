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

## Running your own Pantry

Three opt-in features make it practical to run a Pantry on a home or office network: a Forge that uses a model on your own
hardware, a catalogue that shows the public Pantry and your own packages together, and a way to publish private packages
from a Git repository. None of them changes the public Pantry's behaviour. Each is off until its variable is set, so a
deployment that sets none of them runs exactly as before.

| Feature | Switch | What it does | Needs a key? |
|---|---|---|---|
| [Local LLM for the Forge](#local-llm-for-the-forge) | `FORGE_LOCAL_LLM_ENABLED=true` | The Forge generates packages with any OpenAI-compatible server you run (llama-swap, llama.cpp, vLLM, Ollama). Free, and nothing leaves your network. | Only to *change* the settings, and only if `ADMIN_API_KEY` is set |
| [Upstream mirror](#upstream-mirror) | `PANTRY_UPSTREAM_URL=<public Pantry API>` | One catalogue: your packages first, then the public ones. Nodes need only this address. | No |
| [Private packages](#private-packages) | `ADMIN_API_KEY` set | Publish a package straight from a Git repository, without GitHub login, AI review or container tests. | Yes, always |

`GET /v1/forge/features` reports which of these are on, and the web interface hides what is off:

```json
{ "local_llm": true, "private_packages": true, "upstream_mirror": true }
```

## How it fits together

```
 Jarvis node ──(command_store_url)──┐
                                    ▼
 browser ──▶ jarvis-pantry-web ──▶ jarvis-pantry (:7721) ──▶ PostgreSQL
              (:7720)                │   │  │
                                     │   │  └──▶ upstream Pantry   (PANTRY_UPSTREAM_URL, read-only, cached)
                                     │   └─────▶ GitHub            (clone a repository to add/publish)
                                     └─────────▶ your LLM server   (/v1/models, /v1/chat/completions)
```

- A node installs a package by cloning its GitHub repository. The Pantry only **brokers the lookup** (name, versions,
  repository URL, commit); it never hosts the package contents.
- Your own packages live in the same tables as public submissions, under a reserved author `local`
  (`Local (private)`). That is how the list and detail routes can tell them apart (`"source": "local"`).
- Settings that change at runtime (the local LLM server) live in one table, `app_settings` (migration
  `i6e0f2a4b7c8`), so they survive restarts and can be edited from the web interface. Environment variables are only the
  first-run fallback.

## Local LLM for the Forge

The Forge can build packages with a model on your own hardware instead of a paid API.

**Switch.** `FORGE_LOCAL_LLM_ENABLED` (`true` in the local compose file, **off by default** elsewhere). While it is off the
Settings link is hidden, `/v1/forge/local-llm*` return 404, local models are not listed and the Pantry behaves exactly as
upstream.

**Where the settings come from.** The database wins; the environment fills only what was never saved:

1. **Saved in the web interface (database).** Server address, API key and default model.
2. **Environment, first-run only.** `FORGE_LOCAL_BASE_URL`, `FORGE_LOCAL_API_KEY`, `FORGE_LOCAL_MODEL`. A value that was
   saved as an empty string was cleared on purpose and does not fall back.

The address is normalised on save (whitespace, a trailing `/` and a trailing `/v1` are stripped; it must be `http://` or
`https://`).

**How a model is found.** An administrator's **Detect models** (or Save) calls `GET {server}/v1/models` and the Pantry
stores the list it reports. `GET /v1/forge/models` serves that stored list, so an unauthenticated request never makes the
Pantry connect to the saved server. An unreachable server leaves an empty list rather than an error, so the Forge still
loads. If the server is configured only by environment variables, press **Detect models** once to fill the list.

**Keys and addresses.** The saved API key is only ever sent to the saved address. Probing a different address with
`detect` uses the key in that request (or none), never the saved one.

**How it generates.**

- `GET /v1/forge/models` lists the detected models first, the default at the top, each as `local:<model>` with
  `"provider": "local"`, `"estimated_cost": "free"` and `"default": true` on the chosen one. Hosted models follow.
- Picking `local:<model>` sends the chat to `{server}/v1/chat/completions` with the saved key as a Bearer token. **No
  per-request key is needed.** The bare id `local` is accepted for older clients and resolves to the default (or the
  first detected) model.
- Replies are read with a long timeout (`FORGE_LOCAL_TIMEOUT`, default 900 s, because local models are slow) and any
  `<think>…</think>` block is removed before the reply reaches the Forge. `FORGE_LOCAL_MAX_TOKENS` (default 8192) caps
  the output.

**Using it.**

```bash
cp .env.local.example .env.local            # set PANTRY_DB_PASSWORD (and PANTRY_PUBLIC_API_URL on a server)
docker compose -f docker-compose.local.yaml --env-file .env.local up -d --build
```

Open <http://localhost:7720>, go to **Settings**, enter the server's address (and its API key if it has one), press
**Detect models**, pick a default and save. Over the API:

| Method | Path | Admin key | Purpose |
|---|---|---|---|
| `GET` | `/v1/forge/features` | no | Which optional features are on |
| `GET` | `/v1/forge/local-llm` | no | `{base_url, has_api_key, default_model, source}`. The key itself is never returned. |
| `PUT` | `/v1/forge/local-llm` | when set | Save `{base_url, api_key, default_model}`. `api_key` omitted keeps the stored key; `""` clears it. |
| `POST` | `/v1/forge/local-llm/detect` | when set | Test the supplied address and key, or the saved ones, and return `{ok, models, error}` |
| `GET` | `/v1/forge/models` | no | The Forge's model list, local models first |

**Limits.** The compose file mounts no Docker socket, so the sandboxed container tests used when publishing a submission
are unavailable. Local models are slower and weaker than hosted ones, and the Forge's static checks do not import or run
the generated code: test what it produces.

## Upstream mirror

Set `PANTRY_UPSTREAM_URL` (the local compose file defaults it to the public Pantry's API) and this Pantry lists the public
catalogue **and** its own packages as one store, so nodes need only this address. Unset or empty serves only what it holds,
as upstream does.

- `GET /v1/commands` merges both: your packages first, then the public ones; a local name **shadows** a public one. Each
  item has `"source": "local" | "upstream"`. Search, category and type filters, sorting and pagination apply to the merged
  list, and `/v1/categories` counts both.
- `GET /v1/commands/{name}`, `/versions` and `/download` answer locally first and fall back to the public Pantry.
- The public list is fetched whole (it is small) and filtered here, so both sources behave the same. It is cached for
  `PANTRY_UPSTREAM_TTL_SECONDS` (default 300), with a request timeout of `PANTRY_UPSTREAM_TIMEOUT_SECONDS` (default 8).
- **If the upstream is offline** the last good copy is served, or just your own packages if there never was one. A slow or
  dead upstream never takes the local catalogue down.
  A *download* of an upstream-only package has no cached copy to fall back on, so it answers **502**.
- The mirror is read-only: nothing is written upstream, and nothing from it is stored in your database.

Point a node at it with `command_store_url` in its config, for example `http://pantry.lan:7721`.

## Private packages

The public submission flow needs GitHub login, an AI security review and container tests. For your own repositories there
is an administrator shortcut:

```bash
curl -s -X POST http://pantry.lan:7721/v1/admin/commands/from-repo \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"github_repo_url": "https://github.com/you/jarvis-cmd-thing", "ref": "v1.0.0"}'
```

**What it does.** It clones the repository at `ref` (a tag or branch; the default branch when omitted), validates the
manifest with the same structure check the public flow uses, and publishes it as a local package under the reserved
author `local`. The response gives `command_name`, `version`, `package_type` (`command`, or `bundle` for several
components), `git_commit_sha`, `private_repository` and a `review` field that says in words that none happened.
Re-running it with a new `ref` publishes a new version of the same package.

**What it does not do.** There is **no AI security review and no container test**, because nodes install whatever you
add. So it **refuses to run unless `ADMIN_API_KEY` is set** (403), and answers 401 for a missing or wrong `X-Admin-Key`.

**It never replaces a reviewed package.** If a package with that name already exists and was not added through this
endpoint (a public submission, for example), the request is refused with 409 rather than repointing it at your
repository; rename the package in the manifest, or unpublish the existing one first.

**"Private" means unreviewed and local, not secret.** A package added this way is listed, shown and downloadable through
the same public routes as every other package on this Pantry (`"source": "local"`). Keep the Pantry on a network where that
is acceptable, or put an access-controlled proxy in front of it. Marking a package as not-public per request is not
implemented.

**Private GitHub repositories.** A token is read from the request body (`token`) or `PANTRY_GITHUB_TOKEN`. It is embedded in
the clone URL for the clone only, never stored, and scrubbed from any error text. A node installing a private repository
needs its own access to it.

**Unpublishing.** `DELETE /v1/admin/commands/{name}/local` (admin key) hides a package; it is not deleted, and only
packages added this way can be unpublished here (404 otherwise). Publishing the same name again brings it back.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/admin/commands/from-repo` | Add or update a private package `{github_repo_url, ref?, token?}` |
| `DELETE` | `/v1/admin/commands/{name}/local` | Unpublish a private package |

## The admin key

`ADMIN_API_KEY` is one shared secret sent as the `X-Admin-Key` header, not a login or a JWT. What it gates depends on the
feature:

| Endpoint | With no key set | With a key set |
|---|---|---|
| Local LLM `PUT` and `detect` | **refused** (403) | needs the key |
| `POST /v1/admin/commands/from-repo` and `DELETE …/local` | **refused** (403) | needs the key |
| Existing moderation endpoints (`verify`, `unpublish`, bulk) | as upstream | as upstream |

The endpoints that change what nodes install or where the Pantry sends requests never run on an open Pantry: no key
configured answers 403, a wrong or missing key 401 (constant-time comparison). The Pantry connects to the saved LLM
server (and only the saved server ever receives the saved API key), so keep the API off untrusted networks all the same.
Rotate the key by changing the variable and restarting.

## Run it locally with your own LLM server

`docker-compose.local.yaml` runs everything on one machine:

| Service | Image / build | Port | Notes |
|---|---|---|---|
| `pantry-db` | `postgres:16-alpine` | internal | Data in the `pantry-db` volume; health-checked |
| `pantry` | `Dockerfile.local` | `${PANTRY_PORT:-7721}` | The API; migrations run on start |
| `pantry-web` | `../jarvis-pantry-web` (`PANTRY_WEB_PATH`) | `${PANTRY_WEB_PORT:-7720}` | The web interface |

`.env.local` (copy `.env.local.example`):

| Variable | Default | Purpose |
|---|---|---|
| `PANTRY_DB_PASSWORD` | required | Database password |
| `PANTRY_PUBLIC_API_URL` | `http://localhost:7721` | Address the **browser** uses to reach the API. Baked into the web build, so rebuild after changing it. On a server use its LAN address. |
| `FORGE_LOCAL_LLM_ENABLED` | `true` | Master switch for the local LLM features |
| `PANTRY_UPSTREAM_URL` | public Pantry API | Catalogue to mirror; blank for your own packages only |
| `PANTRY_GITHUB_TOKEN` | blank | Clone private GitHub repositories when adding a package |
| `PANTRY_ADMIN_API_KEY` | blank | Becomes `ADMIN_API_KEY` in the container |
| `FORGE_LOCAL_BASE_URL` / `_API_KEY` / `_MODEL` | blank | First-run defaults; normally set from Settings |

The compose file sets `BYPASS_LLM_KEY=true` (the Forge needs no per-request key for a local server), a Forge rate limit of
60 requests per hour (`FORGE_GENERATE_RATE_LIMIT_PER_HOUR`), and `FORGE_LOCAL_TIMEOUT=900`. One code fix rides along:
SQLAlchemy 2.1 maps a bare `postgresql://` URL to the psycopg v3 driver, so the Pantry rewrites it to
`postgresql+psycopg2://`, the driver this project installs (`DATABASE_URL` normalisation in `app/config.py`).

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

## Requirements

- Python 3.11+
- Docker & Docker Compose (recommended)
- PostgreSQL (`DATABASE_URL`)

## Environment

Copy `.env.example` to `.env`. Key variables:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth for author submissions/management |
| `ADMIN_API_KEY` | Protects admin endpoints; **required** to add private packages |
| `PANTRY_PORT` | Port to serve on (default 7721) |
| `ALERT_WEBHOOK_URL` | Optional Slack/Discord webhook for abuse alerting |
| `BYPASS_LLM_KEY` | Dev: skip the AI-review key requirement |
| `JARVIS_SDK_PATH` | Path to `jarvis-command-sdk` used by container tests |
| `CONTAINER_TEST_TIMEOUT` / `MAX_CONCURRENT_CONTAINER_TESTS` / `MAX_CONCURRENT_CLONES` | Forge validation pipeline tuning |
| `SUBMISSION_RATE_LIMIT_PER_HOUR` / `RATE_LIMIT_DISABLED` | Per-IP submission rate limiting |
| `FORGE_LOCAL_LLM_ENABLED` | Master switch for the local LLM features (default off) |
| `FORGE_LOCAL_BASE_URL` / `FORGE_LOCAL_API_KEY` / `FORGE_LOCAL_MODEL` | First-run defaults for the local server |
| `FORGE_LOCAL_TIMEOUT` / `FORGE_LOCAL_MAX_TOKENS` | Read timeout (900 s) and output cap (8192) for local models |
| `PANTRY_UPSTREAM_URL` | Public Pantry API to mirror (empty = off) |
| `PANTRY_UPSTREAM_TTL_SECONDS` / `PANTRY_UPSTREAM_TIMEOUT_SECONDS` | Cache lifetime (300) and request timeout (8) |
| `PANTRY_GITHUB_TOKEN` | Token for cloning private repositories when adding a package |

## Where the code lives

These features are additive: new modules (`app/services/local_llm.py`, `app/services/upstream.py`,
`app/api/local_commands.py`), one migration (`app_settings`), and small hooks in `app/api/browse.py`,
`command_detail.py`, `download.py`, `forge.py` and `services/llm_client.py`.

## Testing

```bash
pytest
```

The tests for these features are `tests/test_local_llm.py` (settings precedence, model detection, the `local:` model ids, the
feature switch) and `tests/test_upstream.py` (merge order, shadowing, caching, offline fallback).

## License

AGPL-3.0 (see `LICENSE`).
