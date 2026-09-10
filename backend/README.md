# Adversarial Agent MVP Backend

Runnable Phase-0-to-MVP backend for black-box adversarial testing of agent systems. It contains the control plane, durable campaign queue, Red search infrastructure, trusted Blue boundary, stateful virtual services, deterministic verification, capsule supervision, replay, and evidence export. The complete platform bundle also includes the `ui/` operator console and wires it to this API through the Docker Compose profile.

INF-01 includes one synthetic finance reference target, reusable target bundles, and a public scenario catalog. See [Target bundles](docs/TARGET_BUNDLES.md) for building an immutable image, registering its scenario, and testing benign behavior, a known verified consequence, and reset. [INF-02 target inference](docs/TARGET_INFERENCE.md) adds a supervisor-owned broker with pinned operator configuration, episode-scoped access, limits, and private usage accounting while preserving capsule isolation. [INF-03 intervention delivery](docs/INTERVENTION_DELIVERY.md) adds explicit message, document, and tool-response slots with verifiable delivery receipts. The development fixture is not a benchmark result; custom-model training remains separate work.

## Quick start

INF-04–INF-12 adds the [research integration](docs/RESEARCH_INTEGRATION.md): durable
external/managed sessions, the independent [Python SDK](../sdk/README.md), immutable
datasets/checkpoints, approved runtimes, executed evaluations, scoped access and UI
research records. Training stays externally launched; real-model configuration remains
a placeholder. A separate Compose acceptance workflow is included in that guide.

```bash
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn adversarial_agent_mvp.api:create_app --factory --reload
```

The default development database is SQLite. Set `DATABASE_URL` to a PostgreSQL URL in real deployments. Run the worker separately:

```bash
uv run adversarial-worker
```

From the packaged platform root, the complete customer-side stack starts with:

```bash
cd backend
cp .env.example .env
docker compose up --build
```

The operator console is then available at `http://127.0.0.1:3000` and the API at `http://127.0.0.1:8000`. See `../ui/README.md` and `docs/DEPLOYMENT.md` for source-development and security details.

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
