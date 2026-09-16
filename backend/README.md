# Adversarial Agent MVP Backend

See [current project status](../README.md#current-implementation) for delivered scope, controlled
agent coverage, remaining acceptance work and the documentation map.

Runnable Phase-0-to-MVP backend for black-box adversarial testing of agent systems. It contains the control plane, durable campaign queue, Red search infrastructure, trusted Blue boundary, stateful virtual services, deterministic verification, capsule supervision, replay, and evidence export. The complete platform bundle also includes the `ui/` operator console and wires it to this API through the Docker Compose profile.

INF-01 includes one synthetic finance reference target, reusable target bundles, and a public scenario catalog. See [Target bundles](docs/michelle_archive/TARGET_BUNDLES.md) for building an immutable image, registering its scenario, and testing benign behavior, a known verified consequence, and reset. [INF-02 target inference](docs/michelle_archive/TARGET_INFERENCE.md) adds a supervisor-owned broker with pinned operator configuration, episode-scoped access, limits, and private usage accounting while preserving capsule isolation. [INF-03 intervention delivery](docs/michelle_archive/INTERVENTION_DELIVERY.md) adds explicit message, document, and tool-response slots with verifiable delivery receipts. The development fixture is not a benchmark result; custom-model training remains separate work.

## Quick start

INF-04–INF-12 adds the [research integration](docs/michelle_archive/RESEARCH_INTEGRATION.md): durable
external/managed sessions, the independent [Python SDK](../sdk/README.md), immutable
datasets/checkpoints, approved runtimes, executed evaluations, scoped access and UI
research records. Training stays externally launched; real-model configuration remains
a placeholder. A separate Compose acceptance workflow is included in that guide.

For API-only development, run from `backend/`:

```bash
uv sync --extra dev --locked
export DEPLOYMENT_ENVIRONMENT=development
export SERVICE_ROLE=api
export DATABASE_URL=sqlite:///./adversarial_mvp.db
export ARTIFACT_BACKEND=local
export OTEL_ENABLED=false
uv run alembic upgrade head
uv run uvicorn adversarial_agent_mvp.api:create_app --factory --reload
```

These overrides keep a copied Compose `.env` from pointing the host process at the
container-only `postgres` hostname or enabling telemetry without a collector URL.
Existing research authentication settings still apply. Use PostgreSQL for deployed
execution. API startup alone does not provision capsules: episodes need a worker,
matching persistence settings, and a running supervisor configured through
`CAPSULE_SUPERVISOR_URL` and `CAPSULE_SUPERVISOR_TOKEN`. Prefer the
[root Compose runbook](../README.md) for the complete workflow. A separately configured
host worker starts with `SERVICE_ROLE=worker uv run adversarial-worker`.

In a fresh terminal (without the API-only overrides above), the complete local stack
starts from the repository root with:

```bash
cd backend
test -f .env || cp .env.example .env
docker compose up --build
```

The operator console is then available at `http://127.0.0.1:3000` and the API at `http://127.0.0.1:8000`. Review `.env` before starting. See [UI development](../ui/README.md) and [deployment](docs/michelle_archive/DEPLOYMENT.md) for configuration details.

## Validation

```bash
OTEL_ENABLED=false uv run pytest
uv run ruff check .
uv run pyright src
uv build
```

## Security boundary

The target is untrusted. Capsule mode rejects privileged containers, host networking, host mounts, Docker socket access, real secrets, external DNS, metadata endpoints, and direct network routes around Blue. `ALLOW_REAL` is invalid in capsule mode.

The Docker runtime is for development. The Firecracker class is a deliberately fail-closed integration boundary: production pilots must provide an audited runner before it can provision workloads.
