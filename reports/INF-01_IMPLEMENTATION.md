# INF-01 implementation and validation

Date: 2026-09-10.
Specification: `Desktop/AML_Infrastructure_Completion_Plan.md`, INF-01.

## Implemented

- Standalone synthetic finance target with health/invoke/reset endpoints, bounded model/tool execution, and a separate labeled wiring fixture.
- Reusable strict target bundle format with real digest-pinned image references and an operator CLI for build/render, validation, registration, and smoke runs.
- Immutable scenario versions and target bindings, migration `0004`, atomic/idempotent registration, version conflict detection, and frozen scenario family/split membership.
- Public catalog list/detail endpoints with permitted surfaces, operations, and target/task bindings. Private baseline, target configuration, verifier rules, and fixtures are stored separately.
- Worker/adapter integration that resolves the pinned scenario and bootstraps Blue's private state. Target reset receives only its configuration. Verifier and scenario hashes/versions are retained in evidence.
- Reset restores the baseline and clears target conversation/local memory, virtual session/persistent memory, effects, verifier history, and idempotency records. Blue serializes target invocation and reset with a separate lock from tool-effect processing.
- Build/run documentation: `backend/docs/TARGET_BUNDLES.md`.

## Validation performed

- **218 tests passed, 1 skipped; 83.70% coverage**, including the backend and reference target packages.
- The new reference Docker acceptance gate ran against actual images built from current source and cached dependencies. Benign invoice workflow passed; the known external-mail fixture produced verifier-confirmed synthetic PII disclosure; reset restored the baseline.
- Docker containment proof confirmed one internal network with exactly Blue and target members. Test-owned containers and networks were removed after execution.
- SQLite and disposable PostgreSQL 17.6 migrations from `0003` to `0004`, registration, and downgrade passed while preserving existing target records. The disposable PostgreSQL container was removed.
- Public catalog/private-state separation, denied target-token access to private snapshots, task override rejection, bounded model execution, forged-evidence rejection, concurrent episode independence, and reset/invoke ordering passed.
- Ruff and Pyright passed. Source distribution and wheel builds passed; the wheel contains the CLI, target package, and scenario assets. No `.env` was packaged.
- The remaining skipped test is the older externally supplied-image Docker gate. The new reference bundle's live Docker gate passed.

The full validation command used `OTEL_ENABLED=false` because the existing local `.env` enables telemetry without an exporter URL. It supplied `AML_REFERENCE_BUNDLE`, `AML_REFERENCE_BLUE_IMAGE`, and `MVP_TEST_POSTGRES_URL` for the disposable live checks.

## Limits and pending gate

- **Real-model POC validation has not run.** No operator-selected model/endpoint was supplied. The actual model/tool loop and HTTP model protocol are implemented and contract-tested. Scripted and mocked-model results are explicitly not a live-model POC.
- Capsule model connectivity and provider credentials remain INF-02. This implementation does not open an external network path or place provider secrets inside the target.
- Fresh Docker builds were attempted but Docker could not resolve `auth.docker.io`. The documented cached-runtime fallback built and ran successfully. Fresh registry builds still need verification when DNS is available.
- The finance case is labeled `development`; its behavior and eventual benchmark split design still need agreement with the data scientist.

The implementation and deterministic runtime gate are delivered. The phase's real-model POC gate remains pending the configured model run.
