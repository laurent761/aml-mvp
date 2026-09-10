# Research integration (INF-04–INF-12)

The research API and independent Python SDK support externally controlled episodes,
managed Red sessions, immutable data exports, external training metadata, checkpoints,
approved model runtimes, paired evaluations and fresh reproduction. The current UI
reads these same records in its existing navigation.

Training is **externally launched**, as selected for this handoff. The researcher
chooses hardware, trainer, optimizer, model and checkpoint loading. Target model,
provider endpoint and credentials remain **placeholders**. Fixture acceptance proves
the infrastructure wiring; it does not establish real-model behavior or checkpoint
loadability. Follow [target inference](TARGET_INFERENCE.md) when that configuration is supplied.

## Install and connect

From the repository root:

```bash
python3 -m venv .research-venv
.research-venv/bin/pip install ./sdk
```

The SDK needs Python 3.10+, the API URL and a researcher bearer token. It has no
backend imports and needs no Docker, database or supervisor access. See
[SDK examples](../../sdk/README.md) and `sdk/examples/workflows.py`.

Set `RESEARCH_AUTH_REQUIRED=true` on the API for remote use. Configure
`RESEARCH_AUTH_TOKENS` as a JSON object mapping SHA-256 token digests to principals:

```json
{
  "<64 lowercase hex characters: SHA-256 of the bearer token>": {
    "owner_id": "researcher-1",
    "scopes": ["research", "evaluation", "evidence"]
  }
}
```

Generate and distribute tokens through the operator's secret manager. Keep the
plaintext token at the client and the digest at the API. Multiple tokens may share
an owner. Rotation changes token configuration and restarts the API. The `operator`
scope approves runtimes/suites and accesses legacy administration. `evaluation`
permits held-out cases and evaluation operations; `evidence` permits private trace
reads. Ownership remains enforced on research sessions, runs, datasets, checkpoints
and artifacts. The deployment's TLS/reverse proxy should preserve the bearer header.
Health probes remain available without authentication. Production API startup fails
if research authentication is not configured.

The UI connection dialog accepts an optional research token, held in memory. With
the same-origin backend proxy it uses `X-AML-Research-Token`; that explicit header is
translated to the backend bearer header. Unrelated browser authorization is not
forwarded. Changing the connection clears loaded records. Research records are
available even when a researcher lacks access to legacy operator views.

## Sessions, operations and outcomes

All research requests use `X-AML-API-Version: aml.research.v1`. Creation and command
requests require an ASCII `Idempotency-Key` of 1–200 characters. The web process
records a durable command; a leased worker owns the live environment. Both external
sessions and managed linear Red search use that command and persistence path.

| Operation | HTTP surface |
|---|---|
| Public bundles and permitted surfaces | `GET /v1/research-catalog` |
| Create/list/read session | `POST/GET /v1/research-sessions`, `GET /v1/research-sessions/{id}` |
| Reset/step | `POST /v1/research-sessions/{id}/reset`, `POST /v1/research-sessions/{id}/steps` |
| Heartbeat/cancel/close | `POST /v1/research-sessions/{id}/{heartbeat,cancel,close}` |
| Recover durable result | `GET /v1/research-operations/{id}` |
| Paged input/result trajectory | `GET /v1/research-sessions/{id}/trajectory` |
| Private evidence | `GET /v1/research-episodes/{id}/evidence` |

Creation pins the immutable bundle, target, scenario, verifier, baseline reward,
seed, limits, owner and optional runtime/checkpoint. Each reset creates a new episode
and capsule. A step supplies its episode ID and expected next index. The same key
and payload returns the original operation; conflicting reuse fails with 409.
Only one ordinary command may be pending per session. Cancellation can interrupt it.

The result separates `public_observation`, `outcome`, versioned `measurements`,
`baseline_reward` and metered `usage`. Only `public_observation` belongs in automatic
attacker inputs. Unsupported/malformed/unavailable/rejected interventions have
explicit receipts and `invalid_action` status. Delivery uncertainty is not a negative
finding. Outcome success comes from trusted verifier events; shaped reward never
overrides it. A reported custom reward is a separate annotation.

Retain the session and operation IDs when disconnecting. The SDK can recover a
completed result or attach to a still-live session. An uncertain in-flight action
becomes `indeterminate` after worker interruption and is never automatically replayed.
Expired heartbeats, wall limits, cancellation, target readiness loss and supervisor
restart retire sessions. Cleanup is retried through supervisor reconciliation; the
supervisor additionally reaps capsules after their declared timeout plus 60 seconds.
The SDK context manager maintains heartbeats and closes the session on exit.

## Research lineage and exports

| Record | HTTP surface |
|---|---|
| Immutable run configuration | `POST/GET /v1/research-runs` |
| Status, metrics, logs, reported external usage | `POST /v1/research-runs/{id}/events` |
| Exact generation record | `POST /v1/research-runs/{id}/generations` |
| Non-authoritative reward annotation | `POST /v1/research-runs/{id}/rewards` |
| Frozen JSONL/Parquet export | `POST/GET /v1/dataset-snapshots` |
| Export state and checksum manifest | `GET /v1/dataset-snapshots/{id}` |

Generation records preserve supplied prompts, public history, raw response, parsed
action/error, generated/replayed/search-selected provenance, runtime/checkpoint,
tokenizer/template, generation configuration, seed and parent links. Missing data
stays null. Token IDs, masks and log probabilities can be uploaded as trainer artifacts.
Managed calls record the exact HTTP contract request and raw response, including
failed identity checks. They use the existing Red public-context allowlist and disable
strategy memory.

Snapshots freeze selected sessions and a cutoff, then persist their exact records
before writing the artifact. Retries reuse the frozen content. Failed, unused and
unfinished records remain visible with explicit status and source references. The
default `model_input` view excludes official outcomes, verifier measurements and
reported rewards. Select `view=research` to include those separately. Test-containing
runs cannot be exported as training data. Parquet stores schema version plus a raw
JSON column to preserve heterogeneous actions and opaque trainer data. The manifest
contains filters, frozen-content hash, object checksum, size and record count.

Runs link code revision, opaque configuration, dataset IDs, an optional external
tracking URI (including an MLflow run URI), parent/resume references, checkpoints and
evaluations. External lifecycle events are durable API records; the external trainer
owns its tracking process and state restoration. Existing campaign MLflow telemetry
remains available for legacy campaign execution.

## Large artifacts and checkpoints

1. `POST /v1/artifact-uploads` reserves the declared size and SHA-256.
2. `PUT /v1/artifact-uploads/{id}/data` streams bytes into the shared upload volume.
3. `POST /v1/artifact-uploads/{id}/complete` verifies the object and atomically publishes
   the artifact reference. Repeating completion returns the same reference.
4. `POST /v1/checkpoints` records the run, full-model/adapter kind, immutable file
   references, tokenizer, base revision and dependencies. Adapter manifests require
   a base revision. Paths must be safe relative paths.
5. `GET /v1/research-artifacts/{id}/download` streams the owned object; SDK downloads verify
   SHA-256 before replacing the destination.

The API never buffers an entire checkpoint. Local storage uses chunked copies and
atomic replacement; S3 uses multipart upload and streamed reads. Failed transfers
remain incomplete and cannot be referenced by a checkpoint. Retrying a failed transfer
retransmits the full file, rather than resuming at a byte offset. An abandoned active
upload expires; it can also be cancelled before starting a new upload. Finalization
and cancellation cannot release the same reservation twice. API and workers must
share `RESEARCH_UPLOAD_ROOT`. Checkpoint `load_validation=not_asserted` is intentional:
the researcher's serving runtime verifies actual loading.

## Runtime contract and evaluations

An operator registers an immutable `name`/`version` at `POST /v1/model-runtimes` with
endpoint, protocol, logical model, checkpoint ID, generation configuration, limits,
capabilities and an optional environment-variable credential reference. Only trusted
worker processes receive the referenced credential. Campaign inputs select a registry
ID; they cannot supply arbitrary destinations. `POST /v1/model-runtimes/{id}/health`
checks and records health. Registry reads expose the latest check.

The minimal protocol is `aml.attacker.v1`:

```json
{
  "protocol": "aml.attacker.v1",
  "model": "researcher-selected-model",
  "checkpoint_id": "checkpoint_...",
  "action": {"channel": "user_message", "payload": {"text": "..."}},
  "usage": {"input_tokens": 10, "output_tokens": 20}
}
```

`GET /health` echoes protocol/model/checkpoint identity. `POST /generate` receives
those pinned identities plus public context, generation configuration and seed, and
returns the above response. Every response must retain the registered identity.
Changing the endpoint's weights requires a new runtime version. This is identity
attestation by the runtime, not an independent inspection of weight bytes. A critic
is optional. `sdk/examples/fixture_runtime.py` implements a clearly labelled fixture.

Operators freeze suites with `POST /v1/benchmark-suites`: bundle IDs, seeds and equal
per-case limits. `POST /v1/evaluations` supplies baseline/candidate checkpoint IDs and
either registered runtimes or `mode=external`. In external mode, the returned case
sessions are driven with the same SDK. The worker schedules matching pairs, reads
their actual persisted commands and creates an immutable comparison artifact. It
records infrastructure failures separately and disables cross-run strategy memory.
`GET /v1/evaluations/{id}` exposes sessions, progress and executed results.

`POST /v1/research-episodes/{id}/reproduce` creates a fresh pinned replay session.
`GET /v1/research-sessions/{id}/reproduction` reports success, divergence and the
one-attempt reproduction rate. Seeds are recorded inputs, not guarantees of identical
hosted-model responses. Reproduction and benchmark improvement remain separate claims.

## Operations and validation

Owner admission uses a database lock and atomic cost reservations. Limits cover
concurrent sessions, cumulative platform cost, storage, request size/rate, episode
count, steps, tokens, time and heartbeat. Uncertain provider calls retain their
conservative cost reservation; administrators must reconcile those before granting
more budget. Rates of zero are labelled unpriced. External GPU usage is labelled
reported; the platform cannot stop externally launched training. Request rate limits
are per API process; a shared ingress limiter is needed for a deployment-wide rate.

`GET /v1/research-health` reports API, database, supervisor, artifact store and queue
capacity separately. Target readiness is tracked per live session; model health is
checked per registered runtime. The catalog shows registration and the caller's latest
session/error, without presenting fixture registration as real-model acceptance.

The local acceptance profile uses its own database, volumes and loopback ports. Use
a dedicated Docker endpoint for a supervisor: startup reconciliation owns all AML
capsules on that endpoint. Do not run acceptance concurrently with another active
capsule supervisor on the same Docker daemon.

From `backend`, with fresh current API/worker/supervisor/Blue/UI images built:

```bash
uv run python scripts/init_research_smoke.py
docker compose --env-file var/research-smoke.env -f compose.research-smoke.yaml up -d --wait
uv run adversarial-bundle build-reference --output var/bundles/finance-reference.json
docker compose --env-file var/research-smoke.env -f compose.research-smoke.yaml cp var/bundles/finance-reference.json api:/tmp/reference.json
docker compose --env-file var/research-smoke.env -f compose.research-smoke.yaml exec -T api python -m adversarial_agent_mvp.bundle_cli register /tmp/reference.json
uv venv var/sdk-acceptance
uv pip install --python var/sdk-acceptance/bin/python ../sdk
var/sdk-acceptance/bin/python ../sdk/examples/acceptance.py --connection var/research-smoke.client.json --output var/acceptance.json
# Optional Docker Desktop gate: externally launched HTTP attacker fixture.
var/sdk-acceptance/bin/python ../sdk/examples/managed_acceptance.py --connection var/research-smoke.client.json --acceptance var/acceptance.json --endpoint http://host.docker.internal:8099
# Optional destructive fault injection into this acceptance worker only.
var/sdk-acceptance/bin/python scripts/research_fault_acceptance.py --connection var/research-smoke.client.json --output var/fault-acceptance.json
docker compose --env-file var/research-smoke.env -f compose.research-smoke.yaml down -v
```

For a fresh checkout build current images with the main Compose file first (`api`,
`worker`, `capsule-supervisor`, `blue-gateway`, `ui`), following [deployment setup](DEPLOYMENT.md).
The alternative `Dockerfile.cached` files are validation fallbacks when dependency
downloads are unavailable. They require the documented locally cached base images
with matching lockfile versions; they are not portable distribution images. Build
the fallback API as `aml-research-backend:validation`, Blue as
`aml-research-blue:validation`, UI as `aml-research-ui:validation`, and pass
`--cached` to `init_research_smoke.py`. Never commit generated connection files.

Run regressions with `OTEL_ENABLED=false uv run pytest`, `uv run ruff check .`,
`uv run pyright`, `uv build`, and the UI's `npm run lint`, `npm run typecheck`,
`npm test`. Live gates require the disposable PostgreSQL URL and reference image
settings documented in their test modules. The infrastructure completion report at
`reports/INF-04_TO_INF-12_IMPLEMENTATION.md` records the actual validation results.
