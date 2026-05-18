# jarvis-pantry

The **community package store + Forge** for Jarvis. Authors publish commands, agents, device protocols, device managers, and routines as installable packages. Nodes browse, install, and rate them. The Forge uses an LLM (BYOK — bring your own key) to generate skeleton packages from a natural-language description.

> **Identity rule:** pantry is **internet-facing**, runs in the cloud (Fly.io), and is the only Jarvis service that accepts third-party content. Validation paranoia is baked into the pipeline (static analysis + AI security review + sandboxed container tests with `--network=none`). When in doubt: tighten, don't loosen.

---

## What this service is (and isn't)

| Surface | Auth | Used by |
|---|---|---|
| **Browse / search / detail** | None (rate-limited) | Public web, mobile, node CLI |
| **Download** | None (rate-limited) | Node `package_install` flow |
| **Submit / publish** | GitHub OAuth | Authors |
| **Forge generate** | None (BYOK API key) | Authors composing new packages |
| **Reviews** | GitHub OAuth | Authors rating others' packages |
| **Admin moderation** | `X-Admin-Key` | Maintainers |

**Not** a:
- Package *runtime* — nodes execute packages, pantry just delivers them
- Per-tenant store — this is one global catalog; no household scoping
- Continuous deployment pipeline — submissions are one-shot, manual

---

## Topology

```
Authors
   │ GitHub OAuth
   ├──▶ POST /v1/commands/quick-submit (dry-run + confirm)
   │       │
   │       ▼
   │   Submission pipeline (async via job_queue):
   │     1. github_service: clone repo
   │     2. static_analysis: AST + structure validation
   │     3. security_review: BYOK AI review (Claude/OpenAI)
   │     4. container_test: sandboxed (--network=none, 128MB, RO fs)
   │     5. publish → commands table
   │
   │ Forge (browser)
   ├──▶ POST /v1/forge/generate (BYOK)
   │       └─ LLM generates manifest + entry files + README + LICENSE
   ├──▶ POST /v1/forge/create-repo
   │       └─ Creates GitHub repo via OAuth + pushes files
   │
Nodes / Mobile
   │ Browse (public, rate-limited)
   ├──▶ GET /v1/commands, /v1/categories, /v1/commands/{name}
   ├──▶ GET /v1/commands/{name}/download → clone URL + tag
   └──▶ git clone <url> on the node (jarvis-node-setup package_install)

Admins
   ├──▶ POST /v1/admin/commands/{name}/verify (X-Admin-Key)
   └──▶ POST /v1/admin/commands/{name}/unpublish
```

---

## Quick Reference

```bash
# Local dev (port 7721)
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # Set DATABASE_URL, GITHUB_CLIENT_*, ADMIN_API_KEY
.venv/bin/python -m alembic upgrade head
./run.sh

# Docker dev (uses shared jarvis-postgres)
./run.sh --docker

# Production (Fly.io)
./run-prod.sh

# Tests
.venv/bin/pytest tests/ -v
```

Port: **7721** (older docs say 7720 — outdated; `PANTRY_PORT` default is 7721).

---

## Dependency graph

**Upstream (pantry depends on):**
- **PostgreSQL** (required) — six tables (authors, commands, command_versions, security_reports, reviews, submissions)
- **GitHub API** (required for OAuth + repo clone/validation in the submission pipeline; degrades gracefully for browse/download)
- **Docker daemon** (required for `container_test.py`) — runs sandboxed validation containers
- **Claude / OpenAI API** (caller-side BYOK) — submitters/forge users provide their own API key per request; pantry never stores it

**Downstream (consumers):**
- **jarvis-node-setup** — Pantry CLI browse/install/update
- **jarvis-node-mobile** — store UI (planned)
- **jarvis-admin** — store management tab (planned)

**Impact if down:**
- No new package submissions, no Forge generation, no review writes
- Browse/download fails — nodes can't pull packages
- Already-installed packages on nodes keep working

---

## Lifecycle / common operations

### 1. Submission pipeline (the hot path)

```
Author POST /v1/commands/quick-submit
   { github_url, claude_api_key (or openai_api_key) }
                 │
                 ▼
   ┌─────────────────────────────────────────┐
   │ submission_pipeline.process_submission   │
   │                                          │
   │ 1. github_service.clone_repo(github_url) │
   │ 2. github_service.parse_manifest()       │
   │    └─ jarvis_command.yaml (single) OR    │
   │       jarvis_package.yaml (bundle)       │
   │ 3. static_analysis.analyze()             │
   │    └─ AST check, dangerous patterns,     │
   │       shared-dir collision, etc.         │
   │ 4. security_review.review(api_key)       │
   │    └─ BYOK call to Claude/OpenAI         │
   │ 5. container_test.run_tests()            │
   │    └─ docker run --network=none          │
   │       --memory=128m --cpus=0.5           │
   │       --read-only                         │
   │ 6. INSERT into commands + versions       │
   │ 7. (status published)                    │
   └─────────────────────────────────────────┘
                 │
                 ▼
   Author polls GET /v1/submissions/{id}/status
```

The job runs **asynchronously** via `services/job_queue.py`. Author gets the submission ID immediately and polls for status. Each step's results are recorded in `security_reports` and `submissions` tables.

### 2. Forge: generate a package from a description

```
Author POST /v1/forge/generate
   { description, api_key, model (e.g. "claude-3-5-sonnet") }
                 │
                 ▼
   forge_generator.generate()
     ├─ loads SDK authoring spec (auto-generated from jarvis-command-sdk)
     ├─ builds prompt with spec + user description
     ├─ calls Claude/OpenAI with the user's BYOK key
     └─ returns generated files (manifest, entry, README, LICENSE)

Author then POST /v1/forge/create-repo
   (with GitHub OAuth token)
     ├─ creates a new public_repo via GitHub API
     └─ pushes the generated files
```

### 3. Download (the cold path, called by nodes)

```
Node CLI:  jarvis-node-cli install <package_name>
            │
            ▼
   GET /v1/commands/{name}/download
     ├─ rate-limited by IP
     ├─ increments install_count
     └─ returns { clone_url, tag, install_dir }
            │
            ▼
   Node runs: git clone <clone_url> --branch <tag> <install_dir>
   (in jarvis-node-setup's package_install flow — also gated by MQTT push from CC)
```

Pantry doesn't host package content. It hosts metadata + the clone URL. **GitHub is the artifact store.**

---

## "How to..." recipes

### Add a new package type (e.g. `language_pack`)

1. Add a base class to `jarvis-command-sdk` (e.g. `IJarvisLanguagePack`).
2. Update `static_analysis.py`'s type table — required methods + base class check.
3. Update `container_test.py:test_harness.py` — type-specific behavioral test.
4. Update the install-dir mapping table (in this doc + in `jarvis-node-setup`'s install code).
5. Update `forge_generator.py` system prompt so the LLM knows about the new type.
6. Migration: the `commands` table's `components` JSON column is type-agnostic — no schema change usually needed.

### Add a new static analysis rule

`app/services/static_analysis.py`. The analyzer is an AST walker; add a new visitor method or pattern check. Return findings as structured warnings/errors. Errors block publication; warnings don't.

### Add a new validation step

Add a stage to `submission_pipeline.process_submission` and corresponding fields to the `submissions` table. Stages should be **idempotent** — the pipeline retries on transient failures.

### Add a Forge model

`app/services/forge_generator.py` — add the model ID to the `MODELS` registry, set costs in `GET /v1/forge/models`. Submitter pays via BYOK key.

### Stand up a new instance of pantry (e.g. for an org's private store)

Pantry is single-tenant by design — no household scoping. To run a private store, deploy a separate instance with its own DB and GitHub OAuth app. The node-side CLI accepts a configurable Pantry URL.

---

## Invariants & gotchas

1. **BYOK is mandatory for AI review + Forge.** Pantry never holds an LLM API key. The submitter provides their key in the request and pantry passes it through (in-memory only). Don't add a "fallback to server key" path — it'd shift cost to the operator and reduce supply-side incentive to publish quality.
2. **Containers run `--network=none`.** Sandbox is *truly* isolated. If you add a test that needs network (e.g. a smart-home protocol that calls a vendor API), it must be **mocked** at the SDK level — don't loosen the network flag. The `container_test.py` resource caps (128MB / 0.5 CPU / read-only fs) are deliberate and should not be relaxed.
3. **The base test image is cached** (`jarvis-cmd-test-base:latest`). Per-submission builds only copy the repo + install extra deps (~3s). If you change the SDK and bump its pin, **rebuild the base image** or test runs use the old SDK. There's no CI step to detect this drift today.
4. **Static analysis flags shared-dir names that shadow node built-ins.** `services/`, `utils/`, `core/`, etc. are reserved — sys.path-shadowing would break the node. Always use package-specific names like `<pkg>_shared/`. This rule is enforced at publish time.
5. **Bundles are inferred from directory structure if `components` is omitted from the manifest.** `commands/*/command.py`, `agents/*/agent.py`, etc. (see component inference table). Don't depend on inference for ambiguous repos — declare `components` explicitly.
6. **GitHub is the artifact store; pantry stores metadata only.** Deleting a GitHub repo orphans the pantry entry (download will 404 with a friendly error). The pantry doesn't proxy or cache repo content.
7. **Rate limiting is in-memory** (`rate_limiter.py`). A horizontal scale-out would defeat it. Fine for current scale (single Fly.io instance).
8. **`X-Admin-Key` is a shared secret, not a JWT.** Used only for verify/unpublish. Rotate by updating `ADMIN_API_KEY` env on the deployment.
9. **Reviews are one-per-author-per-package.** POST `/v1/commands/{name}/reviews` upserts. If you want history of changes, you'd need a new table.
10. **`download` increments `install_count`** as a side effect of the API call. There's no actual install tracking on nodes — this is "people clicked download," not "people kept it installed."
11. **Port mismatch in older docs.** Some references say 7720; current default is 7721. Meta CLAUDE.md (in `/jarvis/CLAUDE.md`) has 7721.

---

## API surface

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/health` | — | |
| GET | `/v1/commands` | rate-limit | Browse, search, filter by category |
| GET | `/v1/commands/{name}` | rate-limit | Detail + versions + reviews |
| GET | `/v1/commands/{name}/versions` | rate-limit | Version history |
| GET | `/v1/commands/{name}/download` | rate-limit | Returns clone_url + tag; increments install_count |
| POST | `/v1/commands/quick-submit` | GitHub | Two-phase: dry-run + confirm. BYOK API key |
| GET | `/v1/submissions/{id}/status` | rate-limit | Poll pipeline state |
| GET | `/v1/commands/{name}/reviews` | rate-limit | List |
| POST | `/v1/commands/{name}/reviews` | GitHub | Upsert (one per author) |
| DELETE | `/v1/commands/{name}` | GitHub | Owner-only |
| GET | `/v1/categories` | rate-limit | Category list |
| GET | `/v1/forge/models` | — | Available LLM models + costs |
| GET | `/v1/forge/spec` | — | Auto-generated SDK authoring spec |
| POST | `/v1/forge/generate` | — (BYOK) | LLM generates package files |
| POST | `/v1/forge/create-repo` | GitHub | Creates repo + pushes files |
| POST | `/v1/admin/commands/{name}/verify` | X-Admin-Key | Verified badge |
| POST | `/v1/admin/commands/{name}/unpublish` | X-Admin-Key | Soft-delete |
| GET | `/v1/routines/*` | — | Pre-built routine catalog (separate flow) |

---

## Data model (six tables)

| Table | Purpose |
|---|---|
| `authors` | GitHub OAuth users — id, github_id, username, email |
| `commands` | Published packages — name (unique), display_name, description, category, package_type (`command` \| `bundle`), components (JSON), author_id, github_repo_url, install_count, is_verified, is_published |
| `command_versions` | Per-tag version history — command_id, version, tag, manifest (JSON), created_at |
| `security_reports` | AI review output — submission_id, model, findings (JSON), risk_level |
| `reviews` | One-per-author ratings — command_id, author_id, rating (1-5), comment |
| `submissions` | Pipeline state — id, github_url, author_id, status, stage, error, started_at, finished_at |

Migrations: `alembic upgrade head`.

---

## Component types (canonical reference)

| Type | Interface | Install dir on node |
|---|---|---|
| `command` | `IJarvisCommand` | `commands/custom_commands/{name}/` |
| `agent` | `IJarvisAgent` | `agents/custom_agents/{name}/` |
| `device_protocol` | `IJarvisDeviceProtocol` | `device_families/custom_families/{name}/` |
| `device_manager` | `IJarvisDeviceManager` | `device_managers/custom_managers/{name}/` |
| `routine` | JSON (routine.json) | `routines/custom_routines/{name}/` |
| `prompt_provider` | `IJarvisPromptProvider` | `prompt_providers/{tier}/custom/{name}/` (CC) |

Required files in **every** package: manifest (`jarvis_command.yaml` or `jarvis_package.yaml`), entry file(s), `README.md`, `LICENSE`. Forge generates all four. Static analysis warns on missing README/LICENSE.

---

## Config surface

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | yes | — | Postgres |
| `PANTRY_PORT` | no | `7721` | Bind |
| `GITHUB_CLIENT_ID` | yes | — | OAuth |
| `GITHUB_CLIENT_SECRET` | yes | — | OAuth |
| `ADMIN_API_KEY` | yes | — | Admin endpoints |
| `ALERT_WEBHOOK_URL` | optional | — | Slack/Discord webhook for abuse alerts |

No settings DB — config is env-only (cloud service, not multi-tenant per-host).

---

## Architecture

```
app/
├── main.py                    # FastAPI factory, CORS, routers
├── config.py                  # Pydantic Settings
├── db.py                      # SQLAlchemy session
├── models.py                  # 6 tables
├── auth.py                    # GitHub OAuth + admin key check
├── rate_limiter.py            # In-memory IP rate limit
├── api/
│   ├── browse.py              # GET /v1/commands, /v1/categories
│   ├── command_detail.py      # GET /v1/commands/{name}
│   ├── download.py            # GET .../download
│   ├── submit.py              # POST .../quick-submit
│   ├── reviews.py             # /v1/commands/{name}/reviews
│   ├── manage.py              # GitHub OAuth flow + delete
│   ├── forge.py               # /v1/forge/*
│   └── routines.py            # Pre-built routine catalog
└── services/
    ├── github_service.py      # Clone, validate, parse manifest
    ├── static_analysis.py     # AST analyzer + structure validator
    ├── security_review.py     # BYOK AI review (Claude/OpenAI)
    ├── container_test.py      # Sandboxed validation runner
    ├── forge_generator.py     # LLM package generation
    ├── job_queue.py           # Async pipeline queue
    └── submission_pipeline.py # Orchestrates the validation stages
alembic/                       # Migrations
tests/                         # Submission, reviews, forge, browse, rate-limit, static analysis tests
Dockerfile / Dockerfile.fly    # Fly.io deployment
fly.toml                       # Fly.io config
```

---

## Testing

```bash
.venv/bin/pytest tests/ -v
```

Tests cover: submit flow, reviews, browse, command detail, manage, job queue, rate limiter, security review (mocked LLM), static analysis, routines, github service (mocked GitHub API). Container test is exercised through integration tests that mock docker.

---

## Failure modes

| Failure | Behavior |
|---|---|
| Postgres down | All endpoints 5xx |
| GitHub API down | Submission pipeline stalls at clone step; browse/download unaffected |
| Docker daemon unreachable | Submission fails at container_test step; submission marked failed |
| Submitter LLM API key invalid | Security review fails; submission marked failed with error surfaced to author |
| Static analysis errors | Submission rejected; reasons returned in submission status |
| Forge LLM returns malformed YAML | Forge returns error to author; no DB writes |
| Rate limit exceeded | 429 + Retry-After |
| Unpublished package | Browse / download return 404 |

---

## Out of scope / explicitly not here

- **Hosting package binaries.** GitHub is the artifact store.
- **Continuous validation.** A package isn't re-tested when GitHub repo changes — version tags are immutable.
- **Author payments / monetization.** All packages are free. No payment integration.
- **Mirroring.** No backup of repo contents — relies on GitHub uptime.
- **Per-tenant catalogs.** Single global store.
- **Pre-release / staging channels.** Only published vs unpublished. No "beta" channel.
