# INF-04–INF-12 implementation and validation

Date: 2026-09-10. Project: `/Users/michelleberezin/Development/aml-mvp`.

The remaining infrastructure phases are implemented in the moved project. INF-01,
INF-02 and INF-03 source and reports are retained. The researcher launches training
externally. Target model, provider endpoint and credential selection remain placeholders,
as requested. No real-model success or actual checkpoint loading is claimed.

| Phase | Delivered |
|---|---|
| INF-04 | Durable external/managed sessions; pinned configuration; serialized commands; idempotency and expected index; fresh resets; worker leases/fences; heartbeat, cancellation, indeterminate recovery and capsule cleanup. |
| INF-05 | Installable independent `aml-research-sdk` with typed results/errors, async cleanup, bounded retries/concurrency, recovery, streaming transfers, registry/evaluation clients and executable examples. |
| INF-06 | Public observations separated from authoritative outcomes, versioned verifier measurements, baseline rewards, reported reward annotations and usage. |
| INF-07 | Exact generation/provenance records, immutable cutoff snapshots and checksum manifests in JSONL/Parquet; explicit failures/partial data; held-out split protection; private signals excluded from default model inputs. |
| INF-08 | External training lifecycle, code/config/dataset/resume lineage, streamed artifact transfers, atomic completion and immutable full-model/adapter checkpoint manifests. |
| INF-09 | Operator-approved immutable HTTP runtimes, secret references, checkpoint attestation on every response, health records, conservative metering and optional ranking. |
| INF-10 | Frozen suites; matching baseline/candidate cases executed through real sessions; persisted comparisons, infrastructure failures, checkpoint links and fresh reproduction/divergence. |
| INF-11 | Bearer authentication/scopes, resource ownership, database-serialized quota reservations, request/storage/time limits, cleanup and component health/capacity. |
| INF-12 | Research records in existing UI navigation; token connection; trajectories/receipts/outcomes/evidence; runs/datasets/checkpoints/evaluations; deployment and SDK acceptance instructions. |

Implementation entry points are the backend's `research*.py` and `runtime_registry.py`,
the `0005_research_integration` migration, `sdk/`, and `ui/app/research-workspace.tsx`.
The [integration guide](../backend/docs/RESEARCH_INTEGRATION.md) documents contracts,
configuration, limits and reproducible local deployment steps.

## Validation

The final backend run passed **317 tests, with no failures or skips**, including live
Docker, PostgreSQL and MinIO gates. Coverage was **85.67%**, above the required 75%.
Seven existing dependency/configuration deprecation warnings remain. The
[test log](validation/INF-04-12_BACKEND_TESTS.log) and
[JUnit report](validation/INF-04-12_BACKEND_JUNIT.xml) preserve the results.

- All **28 UI tests** passed; UI lint, generated Cloudflare declarations, TypeScript
  checking and production build passed.
- Backend Ruff and Pyright passed. Backend and independent SDK wheel/source distributions built.
- The SDK wheel installed into a new Python environment with only its public dependencies.
- A separate Compose deployment ran API, worker, supervisor, PostgreSQL and UI with
  new credentials, volumes and loopback ports. Real Docker capsules executed benign
  and known-attack fixtures with reset isolation, generation recording, JSONL export,
  checkpoint upload/download, executed paired evaluation and fresh reproduction.
- A separately launched HTTP attacker fixture completed managed Red execution with
  runtime health/identity checks and metered usage. The UI proxy returned the identical
  SDK-created research session records.
- A live worker process was killed during an executing step and restarted. Lease
  recovery retired the session, returned `indeterminate`, and removed both capsule
  containers and its network. The action was not retried.
- A 24 MiB checkpoint passed actual MinIO multipart upload and streamed checksum/size
  verification. PostgreSQL tests covered migrations, concurrent first configuration,
  atomic quota admission and cancellation during a step.

The [saved acceptance evidence](validation/INF-04-12_ACCEPTANCE.json) contains the
fixture run, trajectories, dataset/checkpoint manifests, executed comparison, fresh
reproduction, managed session and worker-crash result. It contains synthetic fixture
data and no credentials. Validation database/container resources are temporary.

Validation found and fixed two existing integration gaps: scenario bootstrap must
precede trusted readiness checks, and supervisor gateway paths must preserve their
leading slash. It also found a capability-token encoding edge case; decoding now
rejects noncanonical base64 encodings of the same signed bytes. Regression checks
cover these fixes. Upload expiry cannot release quota twice, concurrent registration
is serialized, and failed runtime requests preserve their raw responses without reusing
previous-call usage.

## Boundaries

Real target-model acceptance and real checkpoint loading remain unverified because
their configuration is intentionally a placeholder. Training execution stays external.
Runtime checkpoint identity is attested by the serving process; the platform does not
independently inspect its loaded weight bytes. External GPU usage is reported metadata.

Dependency downloads timed out during normal Docker/npm installation. Build validation
used existing local dependency images matching the locked versions, with PyArrow 20
copied from the cached MLflow image. The new SDK installed from its wheel using the
local package cache. A clean network download was not claimed. The cached Dockerfiles
are explicit validation fallbacks; normal deployment Dockerfiles remain available.

The supervisor requires a dedicated Docker endpoint for its capsule inventory and
startup reconciliation. Request rate counters are per API process; aggregate ingress
rate enforcement belongs at a shared proxy. Interrupted provider usage retains a
conservative reservation. These operational boundaries are documented in the guide.
