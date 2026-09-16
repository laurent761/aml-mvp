# AML — Adversarial Agent Research Platform

Run controlled attacks against isolated agents through a web console or Python SDK.
Red chooses attacks, Blue mediates simulated business effects, and verifiers record
outcomes. See the [architecture guide](architecture-guide.html) for the full workflow.

[Run locally](#run-locally) · [SDK](#python-sdk) · [Reproduction](#reproduction) ·
[Development](#development-and-checks) · [References](#references)

## Current implementation

One Python finance reference agent supports scripted `fixture` and configurable
`model` modes. The platform provides durable sessions, recovery, trajectories,
datasets, checkpoints, paired evaluations, and reproduction. Research reset creates
a fresh episode and capsule while retaining previous records.

Real-model acceptance, broader benchmarks, verified checkpoint loading, clean image
build validation, and a production Firecracker/KVM runner remain outstanding.
Training runs externally: researchers supply models, credentials, hardware, and
training/restoration code. Fixture success does not establish model performance.

| Directory | Contents |
|---|---|
| `backend/src/` | API, workers, Red/Blue, storage, verifiers, reference agent, and target protocol |
| `backend/migrations/`, `backend/tests/` | Database migrations and backend checks |
| `sdk/` | Independent Python client and executable examples |
| `ui/` | Web console, API proxy, reusable components, and UI tests |

## Run locally

Requires Docker with Compose v2, Python 3.12+, `uv`, `make`, Node 22.13+ with npm,
and network access for initial installs/builds. Start from the repository root:

```bash
make setup
cd backend
# Review .env; on Linux, set DOCKER_GID to the Docker socket's group.
docker compose up -d --build --wait
uv run adversarial-bundle build-reference --output var/bundles/finance-reference.json
docker compose exec -T api sh -c 'cat > /app/var/uploads/finance-reference.json' < var/bundles/finance-reference.json
docker compose exec -T api python -m adversarial_agent_mvp.bundle_cli register /app/var/uploads/finance-reference.json
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

## Python SDK

Requires Python 3.10+. Install with `pip install ./sdk` from the repository root.
Set `AML_API_URL` and `AML_TOKEN` for an authenticated deployment with a registered
bundle. The SDK has no backend imports and sends protocol version `aml.research.v1`.

```python
import asyncio
import os
from aml_research import Client

async def main():
    async with Client(os.environ["AML_API_URL"], os.environ["AML_TOKEN"]) as client:
        bundles = await client.catalog()
        if not bundles:
            raise RuntimeError("Register a target bundle before running this example.")
        async with client.session(bundle_id=bundles[0]["bundle_id"]) as session:
            initial = await session.reset()
            # Give only initial.public_observation to your attacker.
            result = await session.step({
                "channel": "user_message", "payload": {"text": "Process invoice-001."}
            })
            print(result.outcome)  # Research output, separate from attacker input.

asyncio.run(main())
```

- `client.session()` bounds concurrency and maintains heartbeats. Keep `session.id` and
  `session.last_operation_id`; use `client.operation(id)` to recover results and
  `attach_session()` to resume a live session. `OperationTimeout` exposes its ID.
- Never blindly retry `IndeterminateOperation`. Interruptions retire the episode.
  Retries preserve JSON and idempotency keys; recover operations instead of guessing step indices.
- Failed uploads restart the whole file with the same key; crashed active transfers
  must expire or be cancelled first. Failed transfers cannot create checkpoints.
  Downloads verify SHA-256 before replacing files.
- Outcomes, measurements, rewards, and usage are research outputs. Reported training
  rewards never change verified outcomes.

[Workflow examples](sdk/examples/workflows.py) cover concurrency, exports, training
records, checkpoints, runtimes, and evaluation. Run `python sdk/examples/workflows.py episode`
from the root. Runtime registration requires operator scope; fixture runtimes and
checkpoints are wiring examples. See the [architecture guide](architecture-guide.html)
for lifecycle, isolation, persistence, and model-inference details.

## Reproduction

A finding and a successful fresh replay are separate results; seeds do not guarantee
identical model responses.

| API | Behavior |
|---|---|
| `POST /v1/research-episodes/{id}/reproduce` | Returns 202 with a fresh replay session using the source bundle, policy, seed, and completed actions. Retains source limits with `max_episodes=1`. Requires a finished owned episode, `evaluation` or `operator` scope, and an idempotency key. |
| `GET /v1/research-sessions/{id}/reproduction` | Read progress, success, and observation divergence. |
| `POST /v1/findings/{id}/replay` | Operator replay used by the UI. Send `{"reproduction_only": true, "search_nearby_bypasses": false}` to retain original target/task, policy, attacker configuration, and lineage without starting hardening. |

For authenticated requests, send `Authorization: Bearer <token>`. The SDK sends
`X-AML-API-Version: aml.research.v1`; the API defaults to that version if omitted.
Research reproduction POSTs require an ASCII `Idempotency-Key` of 1–200 characters.
Setting `search_nearby_bypasses` to `true` requests adaptive mutations; a conflicting
`policy_version_id` returns 422. Finding replay returns `replay_campaign_id` and its
execution contract. `reproduction_only` defaults to `false` for compatibility;
the UI always sends `true`. Deploy matching UI/backend versions and apply migrations
through `0005`; the replay flag itself requires no migration.

## Development and checks

Prepare every service from the repository root:

```bash
make setup
```

This installs the locked backend development dependencies, installs the SDK into
the backend virtual environment, installs the locked UI dependencies, and creates
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

**UI**, from `ui/`, with Node 22.13+. Builds/tests also require GNU `timeout` on
`PATH` (on macOS, install Homebrew `coreutils` and add its `libexec/gnubin` directory):

```bash
BACKEND_API_URL=http://localhost:8000 npm run dev
```

Open the printed URL (usually port 5173). Use port 18000 for the authenticated demo.
Enter a research token in the connection dialog; it stays in memory. The same-origin
proxy forwards only `/healthz`, `/readyz`, and `/v1/*`, filters headers, and rejects
redirects. Direct API origins require backend `CORS_ORIGINS`. Views use HTTP polling
and exclude defensive workflows. **Ask AML** requires operator scope; search works
without a model key, while cited answers use a backend-configured model.

**Checks**, from the repository root:

```bash
(cd backend && OTEL_ENABLED=false uv run pytest && uv run ruff check . && uv run pyright && uv build)
(cd ui && npm run lint && npm run typecheck && npm test)
uv build sdk
```

`npm test` includes the production build and rendering, proxy, and UI checks;
`npm run typecheck` generates runtime declarations. From `ui/`, use `npm run build`
to build alone and `BACKEND_API_URL=http://localhost:8000 npm run start` to serve it.
Live infrastructure tests require disposable services; otherwise they skip.
Cached Dockerfiles require matching dependency images and lockfiles.

After editing indexed documentation, refresh Ask AML and restart the API
(rebuild its image for Docker):

```bash
(cd backend && uv run adversarial-guide --repo .. && uv run adversarial-guide --repo .. --check)
```

## References

- [Architecture guide](architecture-guide.html): system design, lifecycle, isolation, persistence, and operational boundaries.
- [UI product scope](ui/PRODUCT.md): research views and outcome interpretation.
