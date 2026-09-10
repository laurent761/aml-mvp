# AML — Infrastructure Completion Plan for the Data Scientist

Date: 2026-09-10
Scope: platform engineering and research integrations only.
Evidence baseline: September 8 aml-mvp-full-codebase.zip, corrected source snapshot.
Status: infrastructure implementation is now recorded in the INF implementation reports.
See [research integration](RESEARCH_INTEGRATION.md) for the delivered APIs, SDK, deployment
and validation workflow. Real-model acceptance remains a placeholder at the user's
request, and training is externally launched. The requirements below are retained as
the original acceptance specification; fixture runs do not satisfy real-model claims.

## 1. The handoff you are building

The data scientist supplies a model client or a Python process that chooses actions. Your platform supplies an executable agent environment, controlled intervention points, independent outcome verification, durable experiment data, and APIs for registering and evaluating the artifacts her code produces.

Her computer, notebook, GPU machine, training framework, and model-serving process are external clients. The platform must not require a specific training algorithm or GPU provider.

| You implement | She decides and implements | Joint contract |
|---|---|---|
| Target packaging, adapters, lifecycle, mock service integration | Attacker architecture, weights, tokenizer, prompting | Public observation and action schemas |
| Target inference connectivity and credentials integration | Training method, optimizer, loss, sampling and reward shaping | Versioned outcome signals and optional reported rewards |
| Episode service, SDK, orchestration, budgets and cleanup | Training script, checkpoint contents and resumption logic | Checkpoint metadata and artifact manifest |
| Evidence, raw experiment exports, storage, access and tracking | Dataset selection, transformations and learning analysis | Benchmark definitions and split assignments |
| Evaluation execution, comparison plumbing and UI | GPU sizing, cloud choice and training/serving topology | Runtime registration and health contract |

You implement measurable forbidden-state predicates from the agreed scenario requirements. She cannot change benchmark ground truth merely by changing her training reward.

## 2. What already exists

These are source-level findings, not proof that the complete live deployment works.

| Existing component | Source evidence | Treatment |
|---|---|---|
| Target registration and immutable versions | contracts.py, storage.py, api.py | Keep; add onboarding validation and runnable bundles |
| Campaign execution and durable work leases | worker.py, orchestrator.py, storage.py | Keep; extend for externally controlled episodes |
| Internal reset/step/close environment | environment.py; ManagedEpisodeEnvironment in orchestrator.py | Extract/reuse behind a research service |
| Linear/adaptive Red execution | red.py, models.py | Keep as optional platform execution mode |
| Capsule supervisor and Docker lifecycle | capsule.py, supervisor.py, services.py | Keep; complete real runtime validation |
| Blue gateway, virtual tools and deterministic verifiers | blue_gateway.py, virtual_world.py, verifier.py | Keep; extend for scenarios and target inference |
| Trajectories, findings, artifacts and model-usage lineage | storage.py, evidence.py, artifacts.py | Keep; extend with raw generation records and dataset snapshots |
| Evaluation aggregation, splits and paired comparisons | evaluation.py | Keep; connect to actual execution and persisted evaluation jobs |
| MLflow and telemetry | telemetry.py | Keep; expose research-run integration |
| Refactored AML UI and reproduction API | ui/, REPRODUCTION.md | Keep; add research read models and validate the latest integrated revision |

Concrete gaps:
- No runnable target agents are supplied in the archive.
- The public API does not expose research sessions with externally driven step execution.
- StepResult uses one done flag rather than distinct termination/truncation/error semantics.
- RedAction.payload is an unrestricted dictionary; supported surfaces need concrete schemas.
- DestinationRoute supports five simulated service types; target inference is not among them.
- The worker constructs one attacker client from operator settings. Campaigns cannot freely select registered immutable runtimes/checkpoints through a supported registry.
- Model usage records hold counts and request hashes; they are not complete training examples.
- Evidence storage exposes whole-byte operations; large checkpoint upload/download needs a suitable streaming path.
- Existing evaluation computes results from supplied samples; it does not by itself run a complete benchmark against her checkpoint.
- Remote research access, per-run ownership, and artifact permissions need an explicit supported boundary. CORS is not authentication.
- Historical reports defer live Docker and PostgreSQL gates; the September 8 replay correction was not fully integrated-tested.

## 3. System relationship

~~~mermaid
flowchart TD
    DS["Her model and training process"] --> SDK["Your Python SDK and research API"]
    SDK --> EP["Episode service and workers"]
    EP --> LAB["Capsule, target and virtual tools"]
    LAB --> VER["Private verifier"]
    EP --> DATA["Trajectories and artifacts"]
    VER --> DATA
    DATA --> SDK
    DS --> REG["Runtime and checkpoint registry"]
    REG --> EV["Evaluation runner"]
    EV --> EP
~~~

The diagram's model/training process is owned by her. Every other node is your integration scope.

## 4. Work packages

### INF-01 — Runnable target bundles and scenario catalog

Add one complete reference target, then make its packaging reusable.

A bundle contains:
- Digest-pinned image, entrypoint, health/invoke/reset contract.
- Model-driven agent loop and legitimate task behavior.
- Tool endpoints wired to existing virtual services.
- Scenario assets and initial virtual-world state.
- Declared attack surfaces and allowed operations.
- Forbidden-state definitions, verifier version and expected benign behavior.
- Target configuration and scenario family/version/split identifiers.

Implement the target's mechanical integration; agree on its intended behavior and benchmark design together. Keep the target implementation hidden from the attacker at runtime.

Separate public scenario description from private ground-truth data. A scenario version must change when initial state, intervention semantics or verifier rules change.

Pass condition: a legitimate task works, a known fixture produces the expected verified consequence, and a reset clears conversation, memory and world effects. A scripted fixture proves wiring only; actual model-driven target runs are required for the POC.

### INF-02 — Target inference integration

The target needs a model connection separately from the attacker's model connection.

Implement a trusted inference broker/adapter with operator-configured provider or local endpoint, request correlation, explicit limits, timeouts and metering. The endpoint/model are supplied configuration; model selection is not your decision.

Keep provider credentials outside the target container. Target requests use restricted episode-scoped access. Do not treat inference as a simulated payment/mail operation.

The current target and Blue run on an internal capsule network. Implement a supported relay/route for inference and update containment validation accordingly. Setting an API-key environment variable or disabling isolation does not complete this work. Preserve the virtual-only routing of business side effects.

Record target-model configuration, parameters and usage. Where providers do not expose an immutable model revision, record the resolved information available and mark reproducibility limitations.

Pass condition: a real target obtains model responses and calls simulated tools, without an uncontrolled network route or access to provider credentials.

### INF-03 — Explicit intervention delivery

For each supported surface, define payload schema, size limits, delivery point and result:
- user_message: a conversational input.
- uploaded_document: a named document consumed at a declared workflow point.
- simulated_tool_result: a replacement for a declared tool-response slot, bounded by the scenario.

Record whether the requested intervention was applied, which slot it affected and which content was delivered. Distinguish unsupported surface, malformed action, unavailable slot and target rejection.

A tool-result intervention must never let the attacker fabricate trusted verifier events or virtual-world truth.

Pass condition: an action has a verifiable delivery receipt and cannot alter unrelated surfaces.

### INF-04 — Research sessions and externally controlled episodes

Add a session service over existing lifecycle, persistence and budget components. An API request enqueues/dispatches work to its episode owner; the web process should not directly own long-lived target execution.

Two modes must share the same execution path:
1. External control: her process generates each action and calls step.
2. Managed campaign: the existing Red runner calls a registered attacker runtime.

At session creation, pin target/scenario/verifier versions, limits, owner and execution mode. Each reset creates a new episode record; it must not overwrite an earlier trajectory. Scenario reset starts from the declared initial state, not the final state of the previous attempt.

Implement serialized per-episode commands, expected step index, idempotency key and payload hash. Repeating the same completed request returns its recorded result; a conflicting reuse fails. After a crash with an uncertain side effect, report an indeterminate outcome and retire/reconcile the episode instead of blindly retrying the action.

Add session heartbeat, lease expiry, fencing of stale workers, cancellation, cleanup and reconciliation. Supervisor restart may invalidate live sessions; report interruption rather than promising transparent continuation.

Pass condition: her process can create, reset, step, disconnect, recover completed results and close. Killing it does not leave indefinite capsules.

### INF-05 — Versioned Python SDK and examples

Provide a small installable client package with:
- Catalog listing, session creation and async context-managed cleanup.
- reset, step, status, heartbeat, cancel and close.
- Typed results/errors, explicit API version and bounded retries.
- Bounded concurrent episodes and grouping identifiers for related attempts.
- Trajectory queries, dataset export, artifact registration and evaluation submission.

Do not give the client Docker access, supervisor tokens or database credentials.

Ship examples for one episode, concurrent episodes, raw export, external runtime registration, and checkpoint evaluation. A contract fake is useful for her development; label it clearly as a fixture.

Pass condition: a fresh Python environment can run the examples without importing private backend modules or editing backend code.

### INF-06 — Outcomes and reward integration

Return separate public and research views:
- PublicObservation: attacker-visible messages, tool results and permitted errors.
- Outcome: terminal success, termination reason, truncation reason and execution status.
- Verifier measurements: versioned objective signals and evidence references.
- Usage: steps, model tokens where known, latency and spend.

The current platform reward can remain available as a named/versioned baseline. Expose raw measurements so she can compute a different training reward. Accept her reported reward and configuration hash as a separate research annotation, not authoritative ground truth.

SDK examples must explicitly pass only the public view to the attacker. Hidden signals must not leak through automatic prompt construction or dataset exports.

Pass condition: changing her reward function does not change recorded target outcomes or official evaluation success.

### INF-07 — Research recording and dataset snapshots

Extend existing lineage with exact generation inputs/outputs when available:
- Prompt messages, public history, raw model response and parsed action.
- Parsing errors, delivery receipt, target observation and resulting outcome.
- Newly generated vs replayed vs search-selected provenance.
- Model/checkpoint/runtime, tokenizer/template and generation-config references.
- Scenario/target/verifier/reward versions, seeds and parent run/episode links.

For externally generated actions, let her submit generation records with stable IDs. Mark absent data as absent rather than reconstructing it. Token IDs, masks and log probabilities are generated by her model/trainer when needed; your side stores/exports them or their artifact references.

Offer raw JSONL/Parquet snapshots with immutable manifests, checksums, filters and schema versions. Include failures and partial runs with explicit status. Do not select her training examples or impose a particular trainer's dataset shape.

Persist agreed split/family membership and reject accidental export of test records into a train-scoped dataset. Keep private evidence separate from the default model-input view.

Pass condition: every exported record has source lineage, and repeated reads of a snapshot return identical content.

### INF-08 — Research-run and checkpoint artifacts

Add a research-run record that can connect:
source code revision → configuration → dataset snapshot → external training-run reference → checkpoint → evaluations.

Her training process reports status, metrics, logs and produced artifacts through supported clients. You provide metadata validation and durable storage, not the optimizer or training state implementation.

Support an execution integration contract as well. In externally launched mode, her environment starts the process and reports its lifecycle. If she supplies a deployment specification for platform-launched jobs, implement a thin runner adapter for that selected environment: submit, inspect, stream logs, cancel and restart with a checkpoint reference. The job specification carries her image/entrypoint, code revision, opaque training configuration, requested resources, dataset references and output location. She selects the hardware, dependencies and training command; you implement lifecycle, credentials, artifact mounts and status reconciliation. First support the environment she actually selects rather than a multi-cloud scheduler. A restart passes her checkpoint back to her script; the script remains responsible for restoring model, optimizer and scheduler state.

Support uploading/downloading large files without buffering the whole checkpoint in API memory. Verify uploaded sizes/hashes and finalize manifests atomically. Incomplete uploads must not create usable checkpoints.

Checkpoint metadata may reference a full model or adapter plus required base revision, tokenizer and other dependencies. Preserve the manifest she supplies and validate its completeness; storing it does not prove it can load.

Pass condition: she can register, download and resolve an immutable checkpoint; a failed upload remains incomplete. She verifies actual loading with her runtime.

### INF-09 — Registered attacker runtimes

Add operator-approved runtime registration:
- Endpoint/protocol and credential reference.
- Logical model and immutable checkpoint identity.
- Generation contract, declared capabilities and health status.
- Versioned configuration and usage-reporting support.

Adapt the existing worker's fixed-client construction to resolve an approved runtime per run. Maintain operator control over destinations and credentials; do not accept arbitrary URLs through campaign inputs.

In external-control mode, no hosted attacker endpoint is necessary: her process selects actions locally. In managed mode, support the agreed minimal HTTP schema or existing compatible adapter. Ranking is optional; do not force her to implement a critic to submit actions.

Pin runtime/checkpoint identity for a run. Detect or disallow switching an active endpoint to different weights without a new runtime version.

Pass condition: her model can be used both from her own loop and through a managed campaign without rebuilding the backend.

### INF-10 — Evaluation execution and reproduction

Build an execution layer around existing evaluation.py:
- Register agreed benchmark suites and frozen split assignments.
- Run baseline/candidate across matching target versions, scenario cases and budgets.
- Accept managed runtimes or externally driven evaluation sessions.
- Persist pair IDs, configurations, outcomes, infrastructure failures and usage.
- Reproduce findings in fresh episodes using pinned conditions.
- Produce comparison artifacts and connect them to checkpoints.

The data scientist defines the experimental design and interpretation. The platform enforces the chosen execution conditions and computes agreed measurements. Pin or disable cross-run strategy memory to avoid contaminating comparisons and held-out data.

Keep three questions distinct: did a forbidden state occur, can the attack reproduce, and is the candidate better across a benchmark? High shaped reward alone answers none of them.

Report stochastic divergence and reproduction rates. Seeds are recorded inputs, not a guarantee of identical hosted-model outputs.

Pass condition: submit two checkpoint references and a suite, retrieve paired results backed by executed episodes. No manually fabricated metric rows.

### INF-11 — Remote access, operations and cost controls

Add authenticated research access with ownership checks for sessions, runs, datasets and artifacts. Use explicit scopes for operator-only resources and evaluation data. Integrate with the deployment's authentication layer; CORS alone is insufficient.

Reuse work leases and telemetry for bounded parallelism, per-owner quotas, request limits, timeout handling, queue depth and capacity reporting. Reserve budget atomically before concurrent work; reconcile actual usage afterward.

Meter target inference and platform-managed attacker calls. For her external compute, accept reported usage and label it as reported. Your platform can limit its own services and refuse new work; it cannot guarantee stopping a GPU machine it does not control.

Expose health separately for API, database, supervisor, target readiness, model endpoint and artifacts.

Pass condition: two clients cannot cross-control sessions; retries do not multiply effects; cancellation releases resources; failures are visible and correctly classified.

### INF-12 — UI and deployment integration

Keep the current AML navigation. Extend read models for:
- Target readiness and onboarding errors.
- Externally controlled runs and live episodes.
- Trajectories, delivery receipts, outcomes and evidence.
- Dataset snapshots, registered checkpoints and evaluations.
- Actual usage and infrastructure failures.

The UI must consume the same records as the SDK. It should not become the place she configures model architectures or training hyperparameters.

Package the backend, SDK documentation, example target and smoke-run instructions. Your deployment supports connecting to her selected environment. GPU/provider selection, trainer dependencies and training-serving layout remain hers.

Pass condition: an SDK-created run appears accurately in the UI, and a fresh deployment completes the documented real-target workflow.

## 5. Proposed public API additions

Names below are proposals, not endpoints already implemented. Reuse existing target/campaign/episode/finding/artifact reads where appropriate.

| Operation | Proposed surface |
|---|---|
| List pinned scenarios and permitted surfaces | GET /v1/scenarios |
| Create a research session | POST /v1/research-sessions |
| Get session state | GET /v1/research-sessions/{id} |
| Heartbeat | POST /v1/research-sessions/{id}/heartbeat |
| Reset into a new episode | POST /v1/research-sessions/{id}/reset |
| Submit one ordered action | POST /v1/research-sessions/{id}/steps |
| Read an asynchronous operation/result | GET /v1/research-operations/{id} |
| Cancel or close | POST /v1/research-sessions/{id}/cancel or /close |
| Create/update external research-run status | POST /v1/research-runs and /{id}/events |
| Submit generation provenance | POST /v1/research-runs/{id}/generations |
| Optional platform-launched external job | POST /v1/research-jobs; GET /v1/research-jobs/{id}; POST /{id}/cancel |
| Export a frozen dataset | POST /v1/dataset-snapshots |
| Read manifest/download references | GET /v1/dataset-snapshots/{id} |
| Initiate/finalize large uploads | POST /v1/artifact-uploads and /{id}/complete |
| Register/read checkpoint manifests | POST /v1/checkpoints; GET /v1/checkpoints/{id} |
| Register approved runtime | POST /v1/model-runtimes |
| Register frozen benchmark suite | POST /v1/benchmark-suites |
| Execute/read an evaluation | POST /v1/evaluations; GET /v1/evaluations/{id} |

Long operations return accepted operation IDs. SDK polling and retry behavior must preserve idempotency. These APIs must never expose the internal capsule supervisor channel.

## 6. Storage and code changes

Reuse the current entities. Proposed new records: ScenarioVersion, ResearchRun, ResearchSession, EpisodeCommand, GenerationRecord, DatasetSnapshot, CheckpointManifest, ModelRuntimeVersion, BenchmarkSuiteVersion and EvaluationRun.

Where existing artifacts, work leases or operational events suffice, use them rather than creating another subsystem. Link each episode to its research run/session and preserve existing campaign lineage where applicable.

Refactor:
- environment.py and contracts.py: clear public/outcome types and action schemas.
- orchestrator.py: reusable managed episode lifecycle shared by both control modes.
- worker.py: dispatch non-campaign work and resolve pinned registered runtimes.
- models.py: raw-generation recording and runtime adapter integration.
- artifacts.py: large streaming/multipart transfer and manifests.
- evaluation.py: retain calculations; add an execution service around them.
- api.py/storage.py: new contracts, entities, migrations and access checks.
- UI read models: expose external research runs without inventing separate state.

Add focused modules/packages for research sessions, scenarios, inference integration, datasets, checkpoint/runtime registry, evaluation execution and the Python SDK. Names and layout are implementation choices, not a rewrite requirement.

## 7. Delivery sequence

| Milestone | Packages | What she can do |
|---|---|---|
| A — First live handoff | INF-01–06; minimal INF-07 and INF-11 | Control one real episode, see trustworthy outcomes, collect data |
| B — Complete research integration | Finish INF-07–09; bounded parallel execution | Export experiments, report her training runs, register and reuse checkpoints |
| C — Evaluation and operational MVP | INF-10; finish INF-11–12 | Compare models reproducibly and operate the workflow through SDK/UI |

Implement and validate one end-to-end vertical slice before broadening target coverage. She can develop against frozen contracts during milestone A and begin actual model experiments at its completion.

## 8. Final handoff acceptance

From a separate Python environment, she can:
1. Authenticate and discover a versioned scenario.
2. Open a session and receive a clean public observation.
3. Send actions selected by her own code.
4. Receive independently verified outcomes with unambiguous stop/error reasons.
5. Run independent episodes concurrently under declared limits.
6. Export complete, versioned research data.
7. Link her external training run and uploaded checkpoint.
8. Select that checkpoint through her runtime for subsequent experiments.
9. Run a benchmark and reproduce findings.
10. Inspect the same results through the existing UI.

Your validation additionally demonstrates cancellation, duplicate-request handling, reset isolation, crash reconciliation, private/public data separation, live PostgreSQL behavior, and live capsule containment.

There is no requirement in this handoff to select her model, GPU, trainer, optimizer, loss, batch size or RL method. The deliverable is a reliable laboratory and integration surface for the decisions she makes.
