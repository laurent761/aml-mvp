# AML implementation status and documentation map

This page describes the checked-in implementation. Deployment health and model
behavior require their own execution evidence. Run the validation commands in the
[root runbook](README.md#development-and-checks) for results from your checkout.

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
| Multiple framework/held-out target agents | Not supplied | Additional target integrations and broader benchmark variants remain research work. One development fixture is not a benchmark suite proving generalization. |
| Learned attacker weights and training | Supplied externally | AML stores run metadata, datasets, checkpoints and evaluation records. It does not train weights or independently verify a runtime's loaded weights. |

Build, register and run the fixture using the [root runbook](README.md).
[Target bundles](backend/docs/michelle_archive/TARGET_BUNDLES.md) and
[target inference](backend/docs/michelle_archive/TARGET_INFERENCE.md) describe model-mode onboarding.
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
[configuration and validation scope](backend/docs/michelle_archive/GUIDE_CHAT.md). This service is
separate from target inference and attacker training.

## Remaining acceptance work

- Select and configure a real target model, run the documented model-mode smoke,
  and retain model provenance and observed outcomes.
- Define and implement additional controlled targets and frozen benchmark cases
  with the data scientist; execute matched comparisons before claiming learning lift.
- Supply trained checkpoints and verify actual loading in the serving runtime.
- Verify clean dependency/image builds without cached fallbacks.
- Supply and independently validate a Firecracker/KVM runner if that isolation
  profile is needed. The repository contains its integration interface only.

## Documentation map

| Document | Purpose |
|---|---|
| [Root README](README.md) | Current setup, fixture acceptance, model handoff and troubleshooting |
| [Interactive guide](architecture-guide.html) | Offline architecture and illustrative episode walkthrough |
| [Backend README](backend/README.md) | Backend development and validation entry points |
| [Target bundles](backend/docs/michelle_archive/TARGET_BUNDLES.md) | Reference agent, scenario packaging, registration and reset semantics |
| [Target inference](backend/docs/michelle_archive/TARGET_INFERENCE.md) | Model connection, credential ownership, limits and model-mode smoke |
| [Intervention delivery](backend/docs/michelle_archive/INTERVENTION_DELIVERY.md) | Supported payloads, slots, receipts and target integration |
| [Research integration](backend/docs/michelle_archive/RESEARCH_INTEGRATION.md) | Sessions, auth, lineage, transfers, runtimes, evaluations and operations |
| [Deployment](backend/docs/michelle_archive/DEPLOYMENT.md) | Compose services, persistence, credentials and external runner interface |
| [Threat model](backend/docs/michelle_archive/THREAT_MODEL.md) | Current trust boundaries and residual risks |
| [Reproduction](backend/REPRODUCTION.md) | Research replay and legacy reproduction-only contracts |
| [SDK README](sdk/README.md) | Client installation, recovery semantics and executable examples |
| [UI README](ui/README.md) / [Product](ui/PRODUCT.md) | UI operation and product semantics |
| [Infrastructure plan](backend/docs/michelle_archive/AML_Infrastructure_Completion_Plan.md) | Original acceptance specification; baseline gaps are historical |

`ui/.21st/DESIGN.md` is a generated design snapshot, paired with
`ui/.21st/design.json`; it is not an implementation-status report. The vendor license
remains the upstream license. Historical specifications and detailed operational
references are retained in `backend/docs/michelle_archive/`.
