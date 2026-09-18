# AML — Adversarial Agent Research Platform

Run controlled attacks against isolated agents through a web console or HTTP API.
Red chooses attacks, Blue mediates simulated business effects, and verifiers record
outcomes. See the [architecture guide](architecture-guide.html) for the full workflow.

[Run locally](#run-locally) · [API access](#api-access) · [Reproduction](#reproduction) ·
[Development](#development-and-checks) · [References](#references)

## Current implementation

One Python finance reference agent supports scripted `fixture` and configurable
`model` modes. The POC console supports target registration, attack campaigns,
experiments, trajectories, verified findings, evidence, replay, and strategy memory.

The backend retains research-session, dataset, checkpoint, and evaluation APIs;
the POC console does not include their record browsers. Existing stored records
remain available through the API. Research reset creates a fresh episode and
capsule while retaining previous records.

Real-model acceptance, broader benchmarks, verified checkpoint loading, clean image
build validation, and a production Firecracker/KVM runner remain outstanding.
Training runs externally: researchers supply models, credentials, hardware, and
training/restoration code. Fixture success does not establish model performance.

| Directory | Contents |
|---|---|
| `backend/src/` | API, workers, Red/Blue, storage, verifiers, reference agent, and target protocol |
| `backend/migrations/` | Database migrations |
| `ui/` | Web console, API proxy, and reusable components |

## Run locally

Requires Docker with Compose v2, Python 3.12+, `uv`, `make`, Node 22.13+ with npm,
and network access for initial installs/builds. Start from the repository root:

```bash
make setup
cd backend
# Review .env; on Linux, set DOCKER_GID to the Docker socket's group.
docker compose up -d --build --wait
uv run adversarial-bundle build-reference --output var/bundles/finance-reference.json
docker compose cp var/bundles/finance-reference.json api:/tmp/reference.json
docker compose exec -T api python -m adversarial_agent_mvp.bundle_cli register /tmp/reference.json
```

Open the **UI at http://localhost:3000** or **API docs at http://localhost:8000/docs**.
These are default ports; `.env` can override them. On Linux, find the Docker socket
group with `stat -c '%g' /var/run/docker.sock`.
The full stack also includes MinIO and MLflow. Reference registration makes the
fixture discoverable; it does not run an experiment or configure a real model.

Use only one active capsule supervisor per Docker endpoint. Stop from `backend/`
with `docker compose down`; adding `-v` deletes stored data.
The default local stack disables research authentication; enable
`RESEARCH_AUTH_REQUIRED` and configure `RESEARCH_AUTH_TOKENS` for authenticated access.
Review the [architecture guide](architecture-guide.html) before remote use.

## API access

Use the web console for the POC workflow. External clients can call the backend
HTTP API directly; its interactive documentation is available at
**http://localhost:8000/docs** after local setup. Adjust the address if your API
runs on a different host or port.

### Authentication

An access token is a secret string that identifies your client and its permissions.
For authenticated HTTP requests, send `Authorization: Bearer <token>`. Use a token
configured by your deployment's operator; an arbitrary token is rejected even when
local authentication is disabled.
For a local authenticated setup, generate a token and its configuration with:

```bash
python3 - <<'PYTHON'
import hashlib
import json
import secrets

token = secrets.token_urlsafe(32)
digest = hashlib.sha256(token.encode()).hexdigest()
identity = {"owner_id": "local-researcher", "scopes": ["research", "evaluation", "evidence", "operator"]}
print("Client token:", token)
print("RESEARCH_AUTH_REQUIRED=true")
print("RESEARCH_AUTH_TOKENS=" + json.dumps({digest: identity}, separators=(",", ":")))
PYTHON
```

Save the printed client token, then replace the two matching settings in
`backend/.env` with the printed configuration lines. The mapping stores the hash;
the client uses the original token. These scopes enable the full local walkthrough.
Apply changes from `backend/` with `docker compose up -d --force-recreate api`.
Enter the same original token in the UI connection dialog when using this
authenticated stack.

## Reproduction

A finding and a successful fresh replay are separate results; seeds do not guarantee
identical model responses.

| API | Behavior |
|---|---|
| `POST /v1/research-episodes/{id}/reproduce` | Returns 202 with a fresh replay session using the source bundle, policy, seed, and completed actions. Retains source limits with `max_episodes=1`. Requires a finished owned episode, `evaluation` or `operator` scope, and an idempotency key. |
| `GET /v1/research-sessions/{id}/reproduction` | Read progress, success, and observation divergence. |
| `POST /v1/findings/{id}/replay` | Operator replay used by the UI. Send `{"reproduction_only": true, "search_nearby_bypasses": false}` to retain original target/task, policy, attacker configuration, and lineage without starting hardening. |

For authenticated requests, send `Authorization: Bearer <token>`. Clients can send
`X-AML-API-Version: aml.research.v1`; the API defaults to that version if omitted.
Research reproduction POSTs require an ASCII `Idempotency-Key` of 1–200 characters.
Setting `search_nearby_bypasses` to `true` requests adaptive mutations; a conflicting
`policy_version_id` returns 422. Finding replay returns `replay_campaign_id` and its
execution contract. `reproduction_only` defaults to `false` for compatibility;
the UI always sends `true`. Deploy matching UI/backend versions and apply migrations
through `0005`; the replay flag itself requires no migration.

## Development and checks

Automated tests and their supporting setup are maintained on the
`tests/project-suite` branch.

Prepare every service from the repository root:

```bash
make setup
```

This installs the locked backend development dependencies and UI dependencies,
and creates
`backend/.env` from the example when it does not already exist.

**API only**, from `backend/` (experiments also need a worker and supervisor):

```bash
export DEPLOYMENT_ENVIRONMENT=development SERVICE_ROLE=api
export DATABASE_URL=sqlite:///./adversarial_mvp.db ARTIFACT_BACKEND=local OTEL_ENABLED=false
uv run alembic upgrade head
uv run uvicorn adversarial_agent_mvp.api:create_app --factory --reload
```

These overrides avoid Compose-only database hostnames. Existing authentication still
applies; use PostgreSQL for deployed execution. A host worker uses matching persistence
and `CAPSULE_SUPERVISOR_URL`/`CAPSULE_SUPERVISOR_TOKEN` settings:
`SERVICE_ROLE=worker uv run adversarial-worker`.

**UI**, from `ui/`, with Node 22.13+. Builds also require GNU `timeout` on
`PATH` (on macOS, install Homebrew `coreutils` and add its `libexec/gnubin` directory):

```bash
BACKEND_API_URL=http://localhost:8000 npm run dev
```

Open the printed URL (usually port 5173).
Enter a research token in the connection dialog; it stays in memory. The same-origin
proxy forwards only `/healthz`, `/readyz`, and `/v1/*`, filters headers, and rejects
redirects. Direct API origins require backend `CORS_ORIGINS`. Views use HTTP polling
and exclude defensive workflows.

**Checks**, from the repository root:

```bash
(cd backend && uv run ruff check . && uv run pyright && uv build)
(cd ui && npm run lint && npm run typecheck && npm run build)
```

`npm run typecheck` generates runtime declarations. From `ui/`, use `npm run build`
to build and `BACKEND_API_URL=http://localhost:8000 npm run start` to serve it.
Cached Dockerfiles require matching dependency images and lockfiles.

## References

- [Architecture guide](architecture-guide.html): system design, lifecycle, isolation, persistence, and operational boundaries.
- [UI product scope](ui/PRODUCT.md): research views and outcome interpretation.
