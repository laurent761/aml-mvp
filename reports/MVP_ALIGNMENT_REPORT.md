# MVP Alignment Report

Date: 2026-09-01  
Reference: `input/backend-mvp-implementation-plan(1).md`  
Scope: benchmark targets, intentionally vulnerable targets, and testing agents remain excluded by request. The operator UI was added as a later explicit requirement even though the original backend plan listed a frontend as excluded.

## Outcome

The implementation now covers the complete buildable platform scope in the plan: typed contracts, durable orchestration, Red v0/v1, Blue and stateful virtual services, deterministic verification, reproducible evidence, policy hardening, capsule supervision, observability, customer-side packaging, and the integrated operator UI.

It is not honest to claim that the original fourteen-item Definition of Done has been fully demonstrated. Three external proof gates remain:

- A live Docker/Firecracker capsule and active network-containment probe could not run because this environment has no Docker CLI/daemon or audited Firecracker runner.
- A disposable live PostgreSQL service was not supplied; the PostgreSQL model and driver are present, while migrations were verified from an empty SQLite database.
- Definition-of-Done item 9 requires held-out vulnerable target variants. Those are testing agents/benchmark targets, which were explicitly excluded, so no adaptive-versus-naive benchmark result is fabricated.

The release status is therefore **implemented with deferred external proof gates**, not an unconditional claim that every empirical gate passed.

## Original Definition-of-Done alignment

| # | Requirement | Status | Evidence |
|---:|---|---|---|
| 1 | Register an opaque OCI image | Implemented and locally verified | Immutable target/version APIs validate digest-pinned manifests, canonicalize image references, preserve entrypoints, and persist typed metadata. Live registry/image launch is part of the Docker gate. |
| 2 | Run the target in a sealed capsule | Implemented; live proof deferred | Docker runtime, authenticated supervisor, resource controls, lifecycle, reconciliation, and a fail-closed external Firecracker runner boundary are implemented. Mocked/runtime-contract tests pass; no daemon is available here. |
| 3 | No uncontrolled external path | Implemented; live proof deferred | Static preflight rejects privileged mode, host networking/mounts, real secrets, external DNS, metadata and direct routes. Runtime topology proof verifies one internal network with only Blue and target. Active live probes remain deferred. |
| 4 | Every MCP/HTTP side effect passes through Blue | Implemented and locally verified | Authenticated Blue ingress, capability scoping, MCP/HTTP normalization, and no direct worker-to-target path are covered by containment/integration tests. |
| 5 | Blue routes effects to stateful virtual services | Implemented and locally verified | Payment, mail, customer, memory, and file services are stateful and exercised through Blue with deterministic outcomes. |
| 6 | Red interacts through the black-box interface only | Implemented and locally verified | Red prompt context uses an explicit public-observation allowlist and excludes verifier state, scalar rewards, policies, and hidden virtual state. |
| 7 | Deterministic verifier detects a virtual forbidden state | Implemented and locally verified | Deterministic verifier events persist actual verifier IDs, severities, evidence references, progress, and terminal signals. |
| 8 | Complete trajectory is reproducible | Implemented and locally verified | Version/seed/action/observation/effect/decision/verifier/model-call lineage, exact replay, hashes, and evidence bundles are persisted and tested end to end. |
| 9 | Adaptive Red beats naive Red on held-out variants | Deferred by explicit scope | The evaluation/ablation framework exists, but the required held-out testing agents were explicitly excluded. |
| 10 | A versioned policy blocks the finding | Implemented and locally verified | Strict immutable policy AST versions, per-episode snapshot pinning, exact replay, and attributable deny/approval proof are tested. |
| 11 | Red searches nearby bypasses | Implemented and locally verified | Source-seeded mutation campaigns, best-first beam search, replay, strategy memory, and bypass lineage are tested. |
| 12 | Benign behavior remains functional | Implemented and locally verified | Hardening requires paired benign actions and observable expectations; successful permitted behavior and absence of forbidden outcomes are evidenced. |
| 13 | Costs, tokens, versions, actions, effects, and evidence are recorded | Implemented and locally verified | Normalized many-to-many model-invocation attribution plus complete campaign/episode/step/effect/artifact lineage is covered by persistence tests. |
| 14 | Containment violations remain zero | Implemented; live proof deferred | Immutable static and runtime containment proof records violation counts and fails closed. A real capsule probe still requires Docker/Firecracker. |

## Missing parts implemented during this audit

- Durable retries with bounded exponential backoff, lease expiry recovery, stale-episode reconciliation, wall-time continuity, and cancellation-safe/idempotent teardown.
- Capsule ownership labels, restart-safe orphan cleanup, runtime boundary inventory, sanitized containment proof, and evidence/API exposure.
- Closed recursive policy ASTs for all planned operators, complexity limits, canonical hashing/deduplication, append-only versions, hot reload for new episodes, and active-episode pinning.
- Typed FastAPI response models and OpenAPI coverage for campaigns, episodes, policies, findings, replay/hardening, artifacts, and metrics.
- Exact causal hardening proof, nearby bypass handling, explicit benign expectations, and correct finding status transitions.
- Exact model-call attribution to every generated/replayed step without leaking private verifier state into Red.
- Digest-pinned target launch semantics and manifest entrypoint enforcement.
- A complete integrated UI for operations, authoring, live campaign inspection, finding hardening, evidence, and system events.

## Validation summary

- Backend: 170 passed, 2 environment-gated skips, 0 failures, 82.32% statement coverage.
- Static quality: Ruff, Pyright, compilation, and `uv lock --check` passed.
- Persistence: all three Alembic revisions applied to an empty database; 22 tables were created and schema drift check passed.
- Packaging: wheel and source distribution built; the wheel installed in a fresh environment and served health/readiness/OpenAPI successfully.
- UI: lint passed; production build passed; 13 tests passed; all eight workspaces and key dialogs were verified in-browser without application console errors.
- Integrated production smoke: UI HTML, `/healthz`, `/readyz`, and `/v1/overview` succeeded through the same-origin UI proxy; a non-allowlisted path was not proxied.
- Consolidated backend harness: `passed_with_deferred_gates` with no required or optional failures.

See `MVP_VALIDATION_REPORT.md` and `acceptance-validation.json` for the execution-level results.
