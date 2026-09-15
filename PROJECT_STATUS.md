# AML implementation status and documentation map

Reviewed against the source on **2026-09-14**. This page describes the checked-in
implementation. Deployment health and model behavior require their own execution
evidence. The latest recorded infrastructure acceptance run is dated 2026-09-10.

## Controlled agents

**One controlled target agent is implemented:** the custom Python finance reference
agent in [aml_reference_target](backend/src/aml_reference_target/agent.py). Its
`finance-invoice-summary` scenario is version **1.1.0**, in the `finance-operations`
family and `development` split.

The legitimate workflow reads an invoice and customer record, writes a session note,
and emails an invoice summary to `finance@internal.test`, omitting personal
identifiers. Business operations use Blue's virtual services. The agent has a bounded
model/tool loop, declared tool allowlists, health/invoke/reset endpoints, and explicit
message, document and tool-result intervention handling.

| Component or mode | Current state | Evidence and limits |
|---|---|---|
| Reference target, `fixture` mode | Implemented; default reference build | Scripted model decisions exercise benign behavior, a known synthetic forbidden consequence and reset. This is integration evidence. |
| Same target, `model` mode | Implemented; requires operator configuration | Uses the supervisor-owned inference broker. Scripted-provider Docker checks passed; no real-model acceptance result is recorded. |
| Target packaging and scenario catalog | Implemented | Digest-pinned bundles, private ground truth, immutable versions and public discovery. Registration does not run or validate an episode. |
| Red attacker infrastructure | Implemented | Legacy campaigns support linear/adaptive search. Research sessions support an external SDK action loop or managed linear Red with an approved HTTP runtime. |
| Blue enforcement and verifiers | Implemented | Policy enforcement, simulated effects and deterministic outcome checks; these are trusted platform services, not additional target agents. |
| Multiple framework/held-out target agents | Not supplied | The atlas's LangGraph/OpenAI Agents SDK targets and broader benchmark variants are design proposals. One development fixture is not a benchmark suite proving generalization. |
| Learned attacker weights and training | Supplied externally | AML stores run metadata, datasets, checkpoints and evaluation records. It does not train weights or independently verify a runtime's loaded weights. |

Build, register and run the fixture using the [root runbook](README.md).
[Target bundles](backend/docs/TARGET_BUNDLES.md) and
[target inference](backend/docs/TARGET_INFERENCE.md) describe model-mode onboarding.
The two target modes use the same agent implementation; they are not two independent
benchmark agents. Target inference defaults to `disabled` until configured.

## Delivered research infrastructure

INF-01–INF-12 implementation is present: target bundles and inference, intervention
receipts, durable sessions and operation recovery, the independent SDK, separate
public observations and research outcomes, immutable exports, streamed checkpoints,
approved runtimes, executed paired evaluations, bearer scopes/ownership/quotas, and
UI inspection of the same research records.

Research reset creates a **new episode and capsule**, retaining prior trajectories.
An in-flight action with an uncertain effect is marked `indeterminate` and its episode
is retired. Within a capsule, reset clears business state but retains inference
history and spend. These are different lifecycle operations.

The UI uses HTTP polling. Its “Verified Exploits” are verifier-backed finding records;
reproduction is a separate recorded result. The current interface excludes policy
management and defensive hardening, although those legacy backend APIs remain.

## Documentation assistant

The console includes **Ask AML**, with local retrieval from the canonical HTML guide
and direct linked files, source citations, and configurable OpenAI-compatible
generation. Exact duplicate passages merge while preserving provenance. A provider
key is required for generated answers; source search works without it. See
[configuration and validation scope](backend/docs/GUIDE_CHAT.md). This service is
separate from target inference and attacker training.

## Recorded validation and remaining work

The [2026-09-10 infrastructure report](reports/INF-04_TO_INF-12_IMPLEMENTATION.md)
records **317 backend tests passed, no skips, 85.67% coverage**, plus **28 UI tests**.
That run included real Docker containers, PostgreSQL, MinIO, SDK/UI integration and
worker-crash recovery. Target inference and attacker services used scripted fixtures.
These counts have not been rerun as part of this documentation review.

Remaining acceptance work:

- Select and configure a real target model, run the documented model-mode smoke,
  and retain model provenance and observed outcomes.
- Define and implement additional controlled targets and frozen benchmark cases
  with the data scientist; execute matched comparisons before claiming learning lift.
- Supply trained checkpoints and verify actual loading in the serving runtime.
- Verify clean dependency/image builds without the cached fallbacks used by the
  recorded acceptance run.
- Supply and independently validate a Firecracker/KVM runner if that isolation
  profile is needed. The repository contains its integration interface only.

## Documentation map

| Document | Purpose |
|---|---|
| [Root README](README.md) | Current setup, fixture acceptance, model handoff and troubleshooting |
| [Interactive guide](architecture-guide.html) | Offline architecture and illustrative episode walkthrough |
| [Backend README](backend/README.md) | Backend development and validation entry points |
| [Target bundles](backend/docs/TARGET_BUNDLES.md) | Reference agent, scenario packaging, registration and reset semantics |
| [Target inference](backend/docs/TARGET_INFERENCE.md) | Model connection, credential ownership, limits and model-mode smoke |
| [Intervention delivery](backend/docs/INTERVENTION_DELIVERY.md) | Supported payloads, slots, receipts and target integration |
| [Research integration](backend/docs/RESEARCH_INTEGRATION.md) | Sessions, auth, lineage, transfers, runtimes, evaluations and operations |
| [Deployment](backend/docs/DEPLOYMENT.md) | Compose services, persistence, credentials and external runner interface |
| [Threat model](backend/docs/THREAT_MODEL.md) | Current trust boundaries and residual risks |
| [Reproduction](backend/REPRODUCTION.md) | Research replay and legacy reproduction-only contracts |
| [SDK README](sdk/README.md) | Client installation, recovery semantics and executable examples |
| [UI README](ui/README.md) / [Product](ui/PRODUCT.md) | UI operation and product semantics |
| [Infrastructure plan](backend/docs/AML_Infrastructure_Completion_Plan.md) | Original acceptance specification; baseline gaps are historical |
| [Mental model atlas](23_MENTAL_MODEL_ATLAS.md) | Original conceptual design with current implementation notes |
| [Reports index](reports/README.md) | Dated implementation/validation evidence and superseded reports |

`ui/.21st/DESIGN.md` is a generated design snapshot, paired with
`ui/.21st/design.json`; it is not an implementation-status report. The vendor license
remains the upstream license. `SOURCE_SHA256SUMS.txt` and the archived JSON/XML
validation artifacts describe their original snapshots; they are not checksums or
test results for the current working tree.
