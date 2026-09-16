# AML — Adversarial Agent Research Platform

AML is a laboratory for testing an agent through controlled interventions, verifying
the resulting effects, and recording reproducible research data. Researchers choose
actions through a Python SDK or let the Red runner call an approved attacker runtime.
Business effects go to simulated services inside an isolated capsule.

**Open the [architecture and lifecycle guide](architecture-guide.html)** for CTOs,
architects and data scientists. The offline page combines the component reference,
system map, intervention walkthrough, recovery semantics and data-to-evaluation
lifecycle. Installation and executable commands are maintained in this README.

**Current scope:** one controlled Python finance reference agent is implemented,
with scripted fixture and configurable model modes. A broader benchmark agent suite
and real-model acceptance are still outstanding. See [project status and the full
documentation map](PROJECT_STATUS.md).

| Directory | Purpose |
|---|---|
| `backend/` | FastAPI, workers, capsule supervisor, Blue gateway, virtual services, verifiers, migrations and deployment |
| `backend/targets/reference/` | Reference target image definitions; source is in `backend/src/aml_reference_target/` |
| `sdk/` | Independent `aml-research-sdk` package and executable examples |
| `ui/` | Research console reading the same records as the SDK |

Real target model, provider endpoint and credentials remain **placeholders**. The
reference fixture works without a selected model. Training is **externally launched**:
the researcher supplies hardware, training code and checkpoint loading.

## Documentation assistant

Open **Ask AML** in the console to search the HTML guide and its linked documents.
Generated answers use an OpenAI-compatible model and inspectable citations. See the
[setup and index-refresh guide](backend/docs/michelle_archive/GUIDE_CHAT.md); source search works
without a model key.

## Prerequisites

- Running Docker Engine or Docker Desktop, with Docker Compose v2.
- Python 3.12+ and `uv` for backend commands. The independent SDK supports Python 3.10+.
- Internet access for the first dependency/image builds.
- Approximately 4 CPU cores, 8 GiB RAM and 20 GiB free disk for a small local setup.
- Node 22.13+ only for UI development outside Docker.

Use one active AML capsule supervisor per Docker endpoint. Startup reconciliation owns
the AML-labelled capsules on that endpoint. If the main stack is already running,
stop it with `docker compose down` from `backend/` before starting the separate profile
below. Its named data volumes are preserved.

## First run: authenticated research demo

This profile starts PostgreSQL, API, worker, supervisor and UI with generated
credentials, separate volumes and loopback ports. Start at the repository root;
the build step enters `backend/`, where the remaining demo commands run.

### 1. Install and build

```bash
cd backend
uv sync --extra dev --locked
docker build --target api -t adversarial-api:local .
docker build --target worker -t adversarial-worker:local .
docker build --target capsule-supervisor -t adversarial-capsule-supervisor:local .
docker build --target blue-gateway -t blue-gateway:local .
docker build -t adversarial-ui:local ../ui
```

Run the first command from the repository root. Backend images share cached build layers.
On Linux, check `stat -c '%g' /var/run/docker.sock` and set `DOCKER_GID` to that group
before starting Compose if it differs from the default `0`.

### 2. Generate credentials and start

```bash
uv run python scripts/init_research_smoke.py
docker compose --env-file var/research-smoke.env -f compose.research-smoke.yaml up -d --wait
docker compose --env-file var/research-smoke.env -f compose.research-smoke.yaml ps
```

The initializer creates `var/research-smoke.env` and `var/research-smoke.client.json`
with restricted permissions. It refuses to overwrite existing files; reuse those
files on subsequent starts. Keep both local.

| Service | Research profile URL |
|---|---|
| UI | <http://127.0.0.1:13000> |
| API | <http://127.0.0.1:18000> |
| OpenAPI documentation | <http://127.0.0.1:18000/docs> |
| Readiness probe | <http://127.0.0.1:18000/readyz> |

In the UI connection dialog, use the same-origin proxy and paste the `token` value
from `var/research-smoke.client.json`. It stays in browser memory. This demo token has
operator permissions for onboarding and suite registration.

This acceptance profile fixes target inference to `disabled`. Use the complete stack
and the target-inference guide below for real-model configuration.

### 3. Build and register a target

```bash
uv run adversarial-bundle build-reference --output var/bundles/finance-reference.json
docker compose --env-file var/research-smoke.env -f compose.research-smoke.yaml cp var/bundles/finance-reference.json api:/tmp/reference.json
docker compose --env-file var/research-smoke.env -f compose.research-smoke.yaml exec -T api python -m adversarial_agent_mvp.bundle_cli register /tmp/reference.json
```

The operator CLI registers the private bundle and immutable image digest. Researchers
see the public scenario in the catalog. Registration does not itself run an episode.

### 4. Install the SDK and run the example

```bash
uv venv var/sdk-acceptance
uv pip install --python var/sdk-acceptance/bin/python ../sdk
var/sdk-acceptance/bin/python ../sdk/examples/acceptance.py --connection var/research-smoke.client.json --output var/acceptance.json
```

The example executes benign and known-attack fixtures, reset isolation, generation
recording, export, checkpoint upload/download, paired evaluation, fresh reproduction
and a UI proxy check. Result IDs are saved to `var/acceptance.json`.

These are scripted wiring checks. The fixture checkpoint is not model weights, and
its results are not evidence of a real model's vulnerability or performance.

Optionally, on Docker Desktop, test a separately launched HTTP attacker fixture:

```bash
var/sdk-acceptance/bin/python ../sdk/examples/managed_acceptance.py --connection var/research-smoke.client.json --acceptance var/acceptance.json --endpoint http://host.docker.internal:8099
```

This starts a loopback fixture server, registers it, runs managed Red, and stops that
server. Other serving environments use the operator-approved runtime workflow in
[the SDK examples](sdk/examples/workflows.py).

### 5. Inspect and stop

Use **Targets** for bundles, **Experiments** for runs/evaluations, **Live Lab** for
sessions, **Trajectories** for actions/receipts, **Learning** for datasets/checkpoints,
**Evidence** for private verification, and **System** for health.

```bash
docker compose --env-file var/research-smoke.env -f compose.research-smoke.yaml logs --tail=100 api worker supervisor
# Stop and preserve data for the next run.
docker compose --env-file var/research-smoke.env -f compose.research-smoke.yaml down
```

Adding `-v` deletes this profile's database, uploads and artifacts. Use it only to
discard the demo; saved record IDs will no longer resolve afterward.

## Complete stack: MinIO, MLflow and telemetry

Use this instead of the research profile when you want the full local stack:

```bash
cd backend
test -f .env || cp .env.example .env
# Review .env; keep development credentials and bindings local.
docker compose up -d --build --wait
docker compose ps
```

Default ports: UI **3000**, API **8000**, MinIO **9000/9001**, MLflow **5000**.
This profile reads `.env`, not `var/research-smoke.env`, and has different volumes.
Its default local configuration does not require research authentication. For SDK
bearer access, configure `RESEARCH_AUTH_REQUIRED=true` and `RESEARCH_AUTH_TOKENS` as
described in [research integration](backend/docs/michelle_archive/RESEARCH_INTEGRATION.md).

Register a reference target with the build/copy/CLI sequence above, using ordinary
`docker compose` without the research profile flags. Copying `.env.example` does not
configure a real model. See [deployment](backend/docs/michelle_archive/DEPLOYMENT.md) for remote access,
credentials, backups and runtime requirements. Stop with `docker compose down`.

## Use your own research process

Install `./sdk` into your Python environment. Set `AML_API_URL` and `AML_TOKEN` for
an authenticated deployment, then run:

```python
import asyncio
import os
from aml_research import Client

async def main():
    async with Client(os.environ["AML_API_URL"], os.environ["AML_TOKEN"]) as client:
        bundles = await client.catalog()
        async with client.session(bundle_id=bundles[0]["bundle_id"]) as session:
            initial = await session.reset()
            # Give only initial.public_observation to your attacker model.
            result = await session.step({
                "channel": "user_message",
                "payload": {"text": "Process invoice-001."},
            })
            print(result.public_observation)
            print(result.outcome)  # Separate research output.

asyncio.run(main())
```

Keep session and operation IDs to recover completed results after a disconnect.
`IndeterminateOperation` means effects are uncertain; retire that episode instead of
submitting the action again. The context manager maintains heartbeats and closes it.

## Connect models and external training

- **Target:** configure supervisor `TARGET_MODEL_*` settings, export its inference
  profile and register a model-mode bundle. The broker keeps credentials outside the
  target. Follow [target inference](backend/docs/michelle_archive/TARGET_INFERENCE.md).
- **Attacker:** choose actions locally through the SDK or register an approved
  `aml.attacker.v1` HTTP runtime for managed Red. Its configuration is separate from
  the target model's configuration.
- **Training:** launch your own process; report lifecycle and metrics, reference
  datasets, and upload checkpoints. Your script owns training and state restoration.
- **Evaluation:** freeze a suite and submit two checkpoint references with managed
  runtimes or externally controlled case sessions. Results come from executed episodes.

See [research integration](backend/docs/michelle_archive/RESEARCH_INTEGRATION.md) for APIs, scopes,
lineage, quotas, checkpoint manifests and reproduction contracts.

## Development and checks

UI development against the running research API:

```bash
cd ui
npm ci
BACKEND_API_URL=http://127.0.0.1:18000 npm run dev
```

Open the Vite port shown in the terminal, usually **5173**. For an API-only development
process, follow the environment overrides in the [backend quick start](backend/README.md#quick-start).
Episodes additionally need the worker and a configured supervisor.
A database hostname such as `postgres` in `.env` is a Compose service name, not a host URL.

```bash
# From backend/
OTEL_ENABLED=false uv run pytest
uv run ruff check .
uv run pyright
uv build

# From ui/
npm run lint
npm run typecheck
npm test
```

Live Docker/PostgreSQL/MinIO tests require explicit disposable-service settings;
otherwise those gates skip. Use the commands above for results from your checkout.

If first-time downloads fail, retry when network access is restored. `Dockerfile.cached`
fallbacks require matching local dependency images; see the integration guide for their
requirements. They are not substitutes for portable clean builds.

## Troubleshooting

| Symptom | Check |
|---|---|
| Empty catalog | Register the target against this deployment's database. |
| 401 | Use this deployment's token; the research demo stores it in its client JSON. |
| 403 | Check research/evaluation/evidence/operator scopes. |
| Session stays queued | Check worker logs, database access and capacity. |
| Reset fails or session interrupts | Check supervisor logs, image availability and socket permissions. |
| 409 on retry | Recover the original operation; do not change its payload/index under the same key. |
| 429 at admission | Check active sessions and owner cost/concurrency limits. |
| Port already allocated | Check which Compose profile is running. |
| Initializer says file exists | Reuse the generated files; it does not rotate credentials implicitly. |
