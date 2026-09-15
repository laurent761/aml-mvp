# INF-02 implementation and validation

> **Phase record.** Research session recovery was subsequently delivered in
> INF-04–INF-12; provider-side transactions and real-model acceptance remain outside
> the recorded proof. See [current status](../PROJECT_STATUS.md).

Date: 2026-09-10.
Specification: [Infrastructure Completion Plan](../backend/docs/AML_Infrastructure_Completion_Plan.md), INF-02 (originally supplied from Desktop).
Workspace: `/Users/michelleberezin/Development/aml-mvp/backend`.

## Delivered

- A trusted supervisor-owned target inference broker with independently configured local/hosted OpenAI-compatible endpoint, model, credentials, parameters, timeouts, request limits, token/cost reservations, and global concurrency limits.
- Immutable credential-free inference profiles pinned in target manifests and checked against the supervisor configuration before provisioning. CLI commands export profiles and render model-mode reference bundles.
- A bounded Blue inference mailbox and supervisor relay through the existing Docker administrative channel. Target and Blue remain the only two members of one internal network; no external network attachment was added.
- Separate signed episode inference capabilities, cross-episode and business/inference scope enforcement, strict message-only requests, no arbitrary URL/header/parameter forwarding, no provider redirects, and no automatic provider retries.
- Correlation-based idempotency, bounded completion delivery retries, sanitized failures, and explicit timeout/cancellation/reservation records. Model mode does not fall back to the deterministic fixture.
- Private usage persistence in existing model invocation and attribution tables, stable IDs preventing duplicate campaign charges, episode/step links, and API/evidence visibility. Recorded metadata includes available resolved model, provider request ID, fingerprint, parameters, usage/cost sources, and reproducibility limitations. No database migration is required.
- Reset retains inference spend and request identity; normal teardown drains inference before evidence generation and removes its relay with the capsule. Runtime health and containment checks include inference support and credential isolation.
- Compose/.env configuration, reference-target transport, CLI integration, README updates, and the operator guide at `backend/docs/TARGET_INFERENCE.md`.

## Validation

The full suite ran from the new workspace with:

```bash
AML_REFERENCE_BUNDLE=var/bundles/finance-reference.json \
AML_REFERENCE_BLUE_IMAGE=aml-inf02-blue:local OTEL_ENABLED=false uv run pytest
```

Result: **247 passed, 3 skipped; 84.24% coverage**. Two additional operator configuration tests added during that run passed separately: credential-free CLI profile/bundle generation and Compose credential ownership. Total distinct passing tests: **249**.

The skipped checks require separately supplied legacy live Docker or disposable PostgreSQL configuration. Existing SQLite migration checks passed. PostgreSQL migration validation from INF-01 remains recorded in its own report.

The new live Docker inference test ran actual model-mode target and Blue containers against an explicitly scripted HTTP provider hosted outside their network. It observed ten provider requests, eight simulated tool effects across benign/attack runs, successful resets, and 150 metered fixture tokens. It verified exactly two members on an internal network, absent provider credentials in both container environments, blocked outbound TCP from the target, successful runtime health, and removal of test-owned resources. The existing INF-01 Docker reference test also passed.

Target and Blue images were built from current source using the documented cached dependency fallback. The target image is pinned in `backend/var/bundles/finance-reference.json`. The inference Blue image used for final validation was `aml-inf02-blue:local`, image ID `sha256:cb06e07ec69c4c6f97061093a1c3110a2d3aee56e2896698994a08020f45967d`.

Ruff, Pyright, source/wheel builds, and `docker compose config --quiet` passed. The wheel contains the inference modules, scenario/catalog code, and reference target, and excludes `.env`.

## Workspace move

The project was moved during finalization from Desktop to Development. The source changes and tests survived. The interrupted documentation patch was applied at the new path, and `uv sync --extra dev --locked --reinstall` refreshed executable paths and the editable package registration. Validation above ran after that repair.

## Remaining acceptance condition

**A real operator-selected model has not been run.** No target model/endpoint was supplied. The scripted provider establishes actual container/HTTP/tool wiring and containment, not model behavior. The real-model acceptance condition remains pending the configured Docker smoke described in the operator guide.

Additional limits: zero pricing rates are explicitly unpriced; unknown provider usage is conservatively reserved; provider aliases/fingerprints are not claimed immutable; abrupt supervisor/host loss can interrupt in-memory audit persistence. The external Firecracker runner and durable provider transactions are outside this Docker implementation.
