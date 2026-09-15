# INF-03 implementation and validation

> **Phase record.** The research-session APIs, idempotency and interruption handling
> listed below as INF-04 work have since been implemented. See
> [INF-04–INF-12](INF-04_TO_INF-12_IMPLEMENTATION.md) and [current status](../PROJECT_STATUS.md).

Date: 2026-09-10.
Specification: [Infrastructure Completion Plan](../backend/docs/AML_Infrastructure_Completion_Plan.md), INF-03 (originally supplied from Desktop).
Workspace: `/Users/michelleberezin/Development/aml-mvp/backend`.

## Implemented

- Explicit `aml.intervention.v1` opt-in and a shared public `aml_target_protocol` package for payload, slot, plan, acknowledgement, and receipt contracts.
- Scenario-bounded message, named-document, and tool-response delivery, with strict schemas and UTF-8 byte limits. The reference scenario advances to **1.1.0**; historical scenarios and legacy message targets retain compatibility.
- Blue-generated delivery receipts with requested/applied slots, exact delivered content and hashes, correlation, evidence source, and distinct unsupported/malformed/unavailable/rejected/unknown statuses.
- Named uploaded documents consumed before the next workflow's first model turn, kept in target memory, and cleared on reset. Uploads do not write virtual files or select arbitrary filesystem paths.
- Tool-result overlays restricted by invocation ID, destination, operation, argument selector, occurrence, and existing string leaf. Replacement text is withheld from conversational input and applied only to the matching public response.
- Original virtual operations, world state, policy decisions, and private verifier events remain authoritative. Response overlays use copies after verifier processing; JSON-looking injected text cannot fabricate trusted events.
- Consistent handling across direct MCP calls, MCP JSON-RPC, and both HTTP tool facades. Cached responses cannot be reused under a different intervention ID, and stale/unrelated calls cannot consume pending replacements.
- Target acknowledgement checks for message/document consumption, rejection of forged receipt fields, and separate execution errors when a target fails after a tool replacement was already applied.
- Receipts retained in private traces, persisted step observations, episode API responses, and evidence exports using existing tables. Replay/novelty ignore receipt identity changes while preserving delivery-semantic comparisons.
- Updated reference target/image packaging, scenario recipe, README, target bundle guide, and `backend/docs/INTERVENTION_DELIVERY.md`.

## Validation

Final full-suite command:

```bash
AML_REFERENCE_BUNDLE=var/bundles/finance-reference.json \
AML_REFERENCE_BLUE_IMAGE=aml-inf03-blue:local OTEL_ENABLED=false uv run pytest
```

Result: **293 passed, 3 skipped; 85.99% coverage**. This includes 44 new intervention tests. The remaining skips require separately supplied legacy Docker or disposable PostgreSQL configuration; SQLite migration checks passed.

The live model-mode Docker test exercised benign conversation and attacks through all three declared surfaces using an explicitly scripted HTTP provider outside the capsule. It verified four applied receipts, twenty provider requests, sixteen simulated tool effects, 300 metered fixture tokens, restored world state after every reset, unchanged virtual files, and healthy runtime cleanup. The existing reference Docker acceptance test also passed.

Containment checks confirmed exactly target and Blue on one internal network, provider credentials absent from both containers, and blocked target outbound TCP. Scripted-provider results validate delivery/inference wiring; they do not establish real-model behavior.

Additional tests cover malformed envelopes and Unicode, exact byte limits, unavailable/unsupported slots, forbidden schema/path/route declarations, target rejection, invalid acknowledgements, timeouts, verifier-forgery attempts, occurrence limits, unchanged unrelated responses, effect idempotency, receipt persistence, and legacy scenario hashes.

Ruff, Pyright, source/wheel builds, and Compose validation passed. The built wheel contains the shared protocol and excludes `.env`. Docker images used the documented cached dependency fallback with current source:

- Target: `sha256:7d9c506ec71b3ea70fcf4ed932710fd34b764992230d9711b4094de536cda743`.
- Blue (`aml-inf03-blue:local`): `sha256:586ef8afb7b8915fc8a7ab375bc8cd901490e459f3638e0fa18a215d39df7446`.
- Rendered fixture bundle hash: `3ead53de170cf31c5609512673acc1ba0a51ceb6da4104509f0b0b5ce28d2de7`.

## Scope

INF-03's delivery acceptance condition is satisfied for the declared surfaces: receipts identify applied content and slots, and substitutions cannot directly alter unrelated surfaces or trusted world/verifier truth. Delivery acknowledgement does not assert that a model obeyed an instruction.

Research-session APIs, action-level idempotency/recovery, and durable handling of abruptly interrupted workers remain INF-04. No real-model validation beyond the previously documented configured-model requirement is claimed.
