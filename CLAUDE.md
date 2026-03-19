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
├── auth.py                    # GitHub OAuth (public_repo scope for Forge)
├── rate_limiter.py            # In-memory rate limiting
├── api/
│   ├── browse.py              # GET /v1/commands, /v1/categories
│   ├── command_detail.py      # GET /v1/commands/{name}
│   ├── download.py            # GET /v1/commands/{name}/download
│   ├── submit.py              # POST /v1/commands (submission + AI review)
│   ├── reviews.py             # GET/POST reviews, admin verify/unpublish
│   ├── manage.py              # GitHub OAuth + command delete (owner-only)
│   └── forge.py               # Forge spec, generate, create-repo endpoints
└── services/
    ├── github_service.py      # Clone, validate structure, parse manifest
    ├── security_review.py     # AI review using submitter's API key (BYOK)
    ├── static_analysis.py     # AST analysis (syntax, methods, dangerous patterns)
    ├── container_test.py      # Docker sandbox testing
    ├── forge_generator.py     # AI package generation (BYOK, 6 models, system prompt from SDK)
    ├── job_queue.py           # Async validation queue (clone → analyze → review → test → publish)
    └── submission_pipeline.py # Orchestrates: clone -> validate -> AI review -> publish
```

## Auth

- **GitHub OAuth** (`read:user,public_repo` scope) — For authors (submissions, reviews, Forge repo creation)
- **IP rate limiting** — For public endpoints (browse, download)
- **Admin key** — For verification/moderation (`X-Admin-Key` header)

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | — | Health check |
| GET | `/v1/commands` | — | Browse/search |
| GET | `/v1/commands/{name}` | — | Detail + versions + reviews |
| GET | `/v1/commands/{name}/versions` | — | Version history |
| GET | `/v1/commands/{name}/download` | — | Clone URL + tag (increments install count) |
| POST | `/v1/commands/quick-submit` | GitHub | Submit package (dry-run + confirm, BYOK AI review) |
| GET | `/v1/submissions/{id}/status` | — | Check submission pipeline status |
| GET | `/v1/commands/{name}/reviews` | — | List reviews |
| POST | `/v1/commands/{name}/reviews` | GitHub | Submit/update review |
| DELETE | `/v1/commands/{name}` | GitHub | Delete package (owner-only) |
| GET | `/v1/categories` | — | Category list |
| GET | `/v1/forge/models` | — | Available LLM models + costs |
| GET | `/v1/forge/spec` | — | Auto-generated SDK authoring spec |
| POST | `/v1/forge/generate` | — | Generate package from description (BYOK) |
| POST | `/v1/forge/create-repo` | GitHub | Create GitHub repo + push files |
| POST | `/v1/admin/commands/{name}/verify` | Admin | Mark verified |
| POST | `/v1/admin/commands/{name}/unpublish` | Admin | Unpublish |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | - | PostgreSQL connection |
| `PANTRY_PORT` | 7721 | API port |
| `GITHUB_CLIENT_ID` | - | GitHub OAuth client ID |
| `GITHUB_CLIENT_SECRET` | - | GitHub OAuth client secret |
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

## Package Bundles

The Pantry supports multi-component bundles via `jarvis_package.yaml`.
A single repo submission can contain commands, agents, device protocols, and device
managers. The validation pipeline analyzes each component by type.

### Manifest schema

```yaml
name: "smart-home-lifx"
display_name: "LIFX Smart Home"
description: "Complete LIFX control"
version: "1.0.0"
components:
  - type: command
    name: turn_lights
    path: commands/turn_lights/command.py
  - type: agent
    name: home_state_agent
    path: agents/home_state_agent/agent.py
```

Repos with only `jarvis_command.yaml` and `command.py` (no `components` field)
are treated as single-command packages automatically.

### Shared code in bundles

Bundle repos often need shared code across components (e.g., a service client used
by both a command and an agent). These should go in a **package-specific directory**:

```
lifx_shared/           # Good — unique name
  lifx_client.py
  helpers.py
```

**Do NOT use** `services/`, `utils/`, `core/`, or other node built-in package names
for shared code. When installed, shared code is added to `sys.path` — using a
built-in name would shadow the node's own packages and break things.

The static analysis pipeline warns on this:
> Shared directory 'services/' shadows a node built-in package. Rename to something
> package-specific (e.g., 'shared/' or 'lib/').

### Component types

| Type | Interface | Install dir on node |
|------|-----------|---------------------|
| `command` | `IJarvisCommand` | `commands/custom_commands/{name}/` |
| `agent` | `IJarvisAgent` | `agents/custom_agents/{name}/` |
| `device_protocol` | `IJarvisDeviceProtocol` | `device_families/custom_families/{name}/` |
| `device_manager` | `IJarvisDeviceManager` | `device_managers/custom_managers/{name}/` |

### Component inference

When `components` is not declared in the manifest, the pipeline infers from repo
directory structure: `commands/*/command.py`, `agents/*/agent.py`,
`device_families/*/protocol.py`, `device_managers/*/manager.py`, or `command.py` at root.

### DB model

The `commands` table has `package_type` (`"command"` or `"bundle"`) and `components`
(JSON array) columns. No table rename — `package_type` distinguishes bundles.

## Validation Pipeline

Submissions go through: **structure validation → static analysis → AI security review → container tests → publish**.

### Static analysis (`static_analysis.py`)

Per-component type checking:

| Type | Base class checked | Required methods |
|------|--------------------|------------------|
| `command` | `IJarvisCommand` | `command_name`, `description`, `parameters`, `required_secrets`, `keywords`, `run`, `generate_prompt_examples`, `generate_adapter_examples` |
| `agent` | `IJarvisAgent` | `name`, `description`, `schedule`, `required_secrets`, `run`, `get_context_data` |
| `device_protocol` | `IJarvisDeviceProtocol` | `protocol_name`, `supported_domains`, `discover`, `control`, `get_state` |
| `device_manager` | `IJarvisDeviceManager` | `name`, `friendly_name`, `description`, `collect_devices` |

Also flags: dangerous imports, raw DB access, SQL mutations, cross-command data access, shared dir name collisions.

### Container tests (`container_test.py`)

Uses a **cached base image** (`jarvis-cmd-test-base:latest`) with the SDK + pyyaml pre-installed.
Per-submission builds only copy the repo + install extra pip deps (~3s for typical packages).

The test harness (`test_harness.py`) runs inside `--network=none` containers with strict
resource limits (128MB RAM, 0.5 CPU, read-only filesystem). It discovers components from
the manifest or infers from directory structure, then runs type-specific behavioral tests.

### AI security review (`security_review.py`)

BYOK (bring your own key) — submitter provides their Claude or OpenAI API key.
Prompt includes interface docs for all 4 component types. For bundles, all component
sources are concatenated with `## Component: {name} ({type})` headers.

## Port: 7720
