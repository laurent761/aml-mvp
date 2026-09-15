# MVP Platform Validation Report

> **Historical snapshot — 2026-09-01.** The scope, results and deferred gates below
> describe that revision. Since then, INF-01 added one controlled finance reference
> target and INF-02–INF-12 added inference and research integration, with recorded
> live Docker/PostgreSQL/MinIO checks. Real-model and held-out benchmark acceptance
> remain outstanding. See [current status](../PROJECT_STATUS.md) and the
> [reports index](README.md) before using this report for planning.

Date: 2026-09-01  
Backend package: `adversarial-agent-mvp-backend 0.1.0`  
Runtime: Python 3.12.13 and Node.js 22+

## Outcome

All locally executable required gates pass. The platform is ready for source handoff and integration with an externally supplied OCI target. Testing agents, benchmark targets, and intentionally vulnerable agents are intentionally absent.

The result is **passed with deferred external gates**. Live Docker/Firecracker isolation, disposable live PostgreSQL, and held-out benchmark performance were not available and are not reported as passes.

## Automated results

| Gate | Result |
|---|---:|
| Backend pytest | 170 passed, 2 skipped, 0 failed |
| Backend statement coverage | 82.32% (75% gate passed) |
| Ruff | Passed |
| Pyright | Passed |
| Python source/test compilation | Passed |
| `uv lock --check` | Passed |
| Consolidated acceptance harness | `passed_with_deferred_gates` |
| Alembic empty-database upgrade | Revisions 0001–0003 passed |
| Alembic drift check | Passed |
| Schema inspection | 22 tables; all expected operational tables present |
| Wheel and source distribution | Passed |
| Fresh wheel installation/import | Passed |
| Installed API health/readiness/OpenAPI | Passed; 36 documented paths |
| UI lint | Passed |
| UI production build | Passed |
| UI tests | 13 passed, 0 failed |
| UI visual-quality detector | No findings |
| Browser workspace/dialog verification | Passed; no application errors |
| UI → backend production proxy smoke | Passed |
| Testing/benchmark agents | Absent, as required |

The only warning is a dependency-level Starlette notice about a future `httpx2` TestClient migration; it does not affect the current runtime or results.

## Validated behavior

- Strict target, task, Red, observation, effect, verifier, capsule, policy-AST, and response contracts.
- Digest-pinned OCI manifests with entrypoint enforcement and fail-closed containment preflight.
- Durable database work leases, heartbeat, retry scheduling, crash recovery, cancellation, stale-episode reconciliation, and persisted wall-time budgets.
- Restart-safe capsule resource labeling, orphan reconciliation, cancellation-safe partial cleanup, and sanitized runtime containment proof.
- Linear Red baseline and adaptive best-first beam search with prefix replay, mutation, novelty, strategy memory, ablations, and attributable model usage.
- Public/private observation separation that withholds verifier, policy, reward, and virtual-state internals from the attacker model.
- Authenticated Blue MCP/HTTP ingress, episode/destination/operation-scoped capabilities, restricted policy evaluation, and disabled `ALLOW_REAL` in capsule mode.
- Stateful payment, mail, customer-data, memory, and file simulators with deterministic forbidden-state verification.
- Immutable campaign, episode, step, effect, decision, verifier, finding, policy, artifact, and operational-event lineage.
- Exact hardening replay with causal Blue-block proof, nearby bypass search, explicit benign-regression expectations, and versioned evidence bundles.
- Local/S3-compatible artifacts with SHA-256 and size verification plus credential-safe MLflow and OpenTelemetry integration.
- Full operator UI for targets, tasks, campaigns, episode traces, findings, hardening, policies, Red configs/strategies, artifacts, and operational events.
- Same-origin UI proxy with strict path/header allowlists, redirect blocking, credential stripping, and direct-origin development fallback.

## Deferred external validation

- Live OCI capsule launch and active DNS/internet/private-CIDR/metadata/host/socket/capability probes: Docker CLI is not installed in this environment.
- Firecracker/KVM: intentionally represented as a fail-closed interface to a separately audited runner; no runner was supplied.
- Live PostgreSQL: `MVP_TEST_POSTGRES_URL` was not supplied; driver import and migration compatibility are validated locally.
- Held-out adaptive-versus-naive Red comparison: requires the explicitly excluded testing/benchmark target variants.

Deferred gates are never treated as successful in this report.
