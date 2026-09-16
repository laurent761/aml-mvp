# AML — Adversarial Agent Research Platform

AML is a laboratory for testing agents through controlled interventions, verifying
what happens, and recording reproducible research data. Use the web console or
Python SDK to run experiments against an isolated target agent.

**Current scope:** one Python finance reference agent, with scripted fixture and
configurable model modes. Real-model acceptance and broader benchmarks remain
outstanding. Researchers supply model configuration, training code, hardware, and
checkpoint loading.

[Implementation](#current-implementation) · [Project layout](#project-layout) ·
[First run](#first-run-authenticated-research-demo) ·
[Full stack](#complete-stack-minio-mlflow-and-telemetry) ·
[Python SDK](#use-your-own-research-process) ·
[Models and training](#connect-models-and-external-training) ·
[Development](#development-and-checks) · [Troubleshooting](#troubleshooting) ·
[References](#references)

For a visual walkthrough, open the [architecture and lifecycle guide](architecture-guide.html).

## Current implementation

The core workflow is **UI or SDK → API → worker → isolated target and Blue services
→ verifier → stored results**. Blue controls business effects through simulated
services; verifiers check whether an experiment reached a forbidden state.

| Area | Implemented behavior |
|---|---|
| Targets | Digest-pinned bundles, immutable scenario versions, private ground truth, and a public catalog. Registration alone does not validate an episode. |
| Attacker (Red) | External SDK action loops or managed linear execution through an approved HTTP runtime. Campaign APIs also support linear/adaptive search. |
| Execution | Isolated capsules, intervention delivery receipts, durable sessions, operation recovery, and scoped access with ownership and quotas. |
| Research records | Separate public observations and verified outcomes; generation records, immutable datasets, streamed checkpoints, paired evaluations, and reproduction. |
| Web console | Shared API records for sessions, runs, trajectories, findings, datasets, checkpoints, evaluations, and health, refreshed through HTTP polling. |

### Finance reference agent

The [reference agent](backend/src/aml_reference_target/agent.py) uses scenario
`finance-invoice-summary` **1.1.0**, in the `finance-operations` family and
`development` split. Its legitimate task reads an invoice and customer record,
writes a session note, and emails a summary to `finance@internal.test` without
personal identifiers. It supports bounded model/tool execution, declared tool
allowlists, health/invoke/reset endpoints, and message, document, and tool-response
interventions.

| Mode | Purpose | Requirements |
|---|---|---|
| `fixture` | Scripted benign behavior, a known synthetic forbidden consequence, and reset checks | Default reference build; no model configuration |
| `model` | The same agent driven by a model through the supervisor-owned inference broker | Operator-supplied provider, model, credentials, and inference profile |

These are two modes of one agent. Fixture checks demonstrate integration; they do
not establish real-model performance or benchmark generalization. Target inference
is disabled until configured. See [target bundles](backend/docs/michelle_archive/TARGET_BUNDLES.md)
and [target inference](backend/docs/michelle_archive/TARGET_INFERENCE.md) for details.

### Lifecycle and outcome semantics

- Research reset creates a **new episode and capsule**, retaining prior trajectories.
  Within a capsule, reset clears business state but retains inference history and spend.
- An action with uncertain effects becomes `indeterminate`; its episode is retired.
  Recover recorded operations after a disconnect rather than resubmitting an action.
- “Verified Exploits” are verifier-backed findings. Reproduction is a separate result.
  The UI excludes defensive hardening and policy administration; their backend APIs remain.
- Training runs externally. AML records metadata and artifacts; it does not train
  weights or independently verify which weights a serving runtime has loaded.

### Remaining acceptance work

- Configure a real target model and run the model-mode smoke with recorded provenance.
- Add controlled targets and frozen benchmark cases; execute matched comparisons
  before claiming learning improvements.
- Supply trained checkpoints and verify loading in the serving runtime.
- Verify clean dependency and image builds without cached fallbacks.
- Supply and validate a Firecracker/KVM runner if required; only its integration
  interface is present.

Use [development and checks](#development-and-checks) for results from your checkout.
Deployment health and model behavior require their own execution evidence.

## Project layout

| Path | Responsibility |
|---|---|
| `backend/src/adversarial_agent_mvp/` | API, workers, Red, Blue, capsule management, storage, verifiers, and Ask AML |
| `backend/src/aml_reference_target/` | Finance reference agent |
| `backend/src/aml_target_protocol/` | Shared target and intervention contracts |
| `backend/targets/reference/` | Reference target Dockerfiles |
| `backend/migrations/` | Database schema migrations |
| `backend/tests/` | Unit, integration, containment, and live infrastructure checks |
| `backend/scripts/` | Research setup and validation utilities |
| `backend/compose*.yaml` | Local service orchestration |
| `backend/docs/michelle_archive/` | Preserved specifications and operational references |
| `sdk/src/aml_research/` / `sdk/examples/` | Independent Python client and research workflows |
| `ui/app/` / `ui/components/` | Web console and reusable interface components |
| `ui/lib/` / `ui/worker/` | API client, research calculations, and backend proxy |
| `ui/tests/` | Interface, rendering, and proxy checks |

## First run: authenticated research demo

This profile starts PostgreSQL, API, worker, supervisor and UI with generated
credentials, separate volumes and loopback ports. Start at the repository root;
the build step enters `backend/`, where the remaining demo commands run.

### Prerequisites

- Running Docker Engine or Docker Desktop, with Docker Compose v2.
- Python 3.12+ and `uv` for backend commands. The independent SDK supports Python 3.10+.
- Internet access for the first dependency/image builds.
- Approximately 4 CPU cores, 8 GiB RAM and 20 GiB free disk for a small local setup.
- Node 22.13+ only for UI development outside Docker.

Use one active AML capsule supervisor per Docker endpoint. Startup reconciliation owns
the AML-labelled capsules on that endpoint. If the main stack is already running,
stop it with `docker compose down` from `backend/` before starting the separate profile
below. Its named data volumes are preserved.

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

Backend images share cached build layers.
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

This profile fixes target inference to `disabled`. For model configuration, use the
[complete stack](#complete-stack-minio-mlflow-and-telemetry).

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

For MinIO, MLflow, telemetry, or configurable target inference, use this profile.
Start from the repository root in a fresh terminal:

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

Install with `pip install ./sdk` from the repository root (Python 3.10+). Set
`AML_API_URL` and `AML_TOKEN` for an authenticated deployment with a registered
bundle, then run:

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

Keep session and operation IDs for recovery. `IndeterminateOperation` signals an
uncertain side effect; do not resubmit it. The session context manager maintains
heartbeats and closes the session. See the [SDK README](sdk/README.md) for recovery
and transfer behavior.

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

## Documentation assistant

Open **Ask AML** in the console to search the guide and linked documents. Search
works without a model key; generated answers require a backend-configured
OpenAI-compatible model and include inspectable citations. Duplicate passages are
merged while preserving source references. This service is separate from target
inference and attacker training. See the [setup guide](backend/docs/michelle_archive/GUIDE_CHAT.md).

## Development and checks

### Local development

From the repository root, start UI development against the running research API:

```bash
cd ui
npm ci
BACKEND_API_URL=http://127.0.0.1:18000 npm run dev
```

Open the Vite port shown in the terminal, usually **5173**. For an API-only development
process, follow the environment overrides in the [backend quick start](backend/README.md#quick-start).
Episodes additionally need the worker and a configured supervisor.
A database hostname such as `postgres` in `.env` is a Compose service name, not a host URL.

### Validation

Run each block from the repository root.

Backend tests, lint, type checks, and packaging:

```bash
(cd backend && OTEL_ENABLED=false uv run pytest && uv run ruff check . && uv run pyright && uv build)
```

UI lint, type checks, production build, and tests:

```bash
(cd ui && npm run lint && npm run typecheck && npm test)
```

SDK packaging:

```bash
uv build sdk
```

Live Docker/PostgreSQL/MinIO tests require explicit disposable-service settings;
otherwise those gates skip. Use the commands above for results from your checkout.

If first-time downloads fail, retry when network access is restored. `Dockerfile.cached`
fallbacks require matching local dependency images; see the integration guide for their
requirements. They are not substitutes for portable clean builds.

### Refresh Ask AML documentation

After changing this README, the HTML guide, or its indexed references, rebuild the
packaged search index and check that it matches the sources:

```bash
(cd backend && uv run adversarial-guide --repo .. && uv run adversarial-guide --repo .. --check)
```

Restart the API to load the new index. For containers, rebuild the API image first.

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

## References

| Document | Purpose |
|---|---|
| [Interactive guide](architecture-guide.html) | Offline architecture and illustrative episode walkthrough |
| [Backend README](backend/README.md) | Backend development and validation entry points |
| [Target bundles](backend/docs/michelle_archive/TARGET_BUNDLES.md) | Reference agent, scenario packaging, registration and reset semantics |
| [Target inference](backend/docs/michelle_archive/TARGET_INFERENCE.md) | Model connection, credential ownership, limits and model-mode smoke |
| [Intervention delivery](backend/docs/michelle_archive/INTERVENTION_DELIVERY.md) | Supported payloads, slots, receipts and target integration |
| [Research integration](backend/docs/michelle_archive/RESEARCH_INTEGRATION.md) | Sessions, auth, lineage, transfers, runtimes, evaluations and operations |
| [Deployment](backend/docs/michelle_archive/DEPLOYMENT.md) | Compose services, persistence, credentials and external runner interface |
| [Threat model](backend/docs/michelle_archive/THREAT_MODEL.md) | Current trust boundaries and residual risks |
| [Ask AML setup](backend/docs/michelle_archive/GUIDE_CHAT.md) | Documentation search, model configuration, and index refresh |
| [Controlled-agent details](backend/docs/michelle_archive/CONTROLLED_AGENTS_HANDOFF_PROMPT.md) | Target tool loop, intervention surfaces, and state ownership |
| [Reproduction](backend/REPRODUCTION.md) | Research replay and legacy reproduction-only contracts |
| [SDK README](sdk/README.md) | Client installation, recovery semantics and executable examples |
| [UI README](ui/README.md) / [Product](ui/PRODUCT.md) | UI operation and product semantics |
| [Infrastructure plan](backend/docs/michelle_archive/AML_Infrastructure_Completion_Plan.md) | Original acceptance specification; baseline gaps are historical |

Specifications and detailed operational references remain in `michelle_archive`.
The generated UI design snapshots in `ui/.21st/` describe the visual design, not
implementation status. Vendored styles retain their upstream license.
