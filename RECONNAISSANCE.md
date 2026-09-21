# AML reconnaissance and Lauren's ML boundary

Inspected revision: `e51494d51d6b11d379ea8e869701bd268ff34c02`.
Repository: https://github.com/michelle-ralph/aml-mvp

This is static implementation inspection, not an executed campaign or a claim that the system passes integration tests. No implementation was changed. The checkout is temporary, at `/private/tmp/aml-mvp-recon`.

## 1. Architecture relevant to ML

The infrastructure is substantially beyond a prompt generator. It has adaptive search, executable simulated services, deterministic verification, durable research sessions, dataset export, checkpoint registration, and paired evaluation. The inspected paths implement inference/search and research bookkeeping; they do not implement parameter training, an optimizer, or a learned-policy update loop.

Paths below are relative to this checkout. Most backend modules live under `backend/src/adversarial_agent_mvp/`.

| Responsibility | Implementation | What it actually does |
|---|---|---|
| Service deployment | `backend/compose.yaml` | API, worker, capsule supervisor, Blue gateway template, UI, PostgreSQL, MinIO, MLflow, OpenTelemetry collector, image registry, migrations. Episode capsules are provisioned dynamically. |
| Campaign API | `api.py:create_app/create_campaign`; `storage.py:Repository.create_campaign` | Validates and persists campaign inputs and queues a campaign work lease. |
| Worker | `worker.py:Worker.run_once/_run_with_heartbeat` | Claims leased jobs, renews leases, dispatches campaign/research-session/dataset/evaluation work, and handles retries. |
| Episode orchestration | `orchestrator.py:CampaignRunner`, `ManagedEpisodeEnvironment` | Resolves task/target/policy, applies budgets, creates environments, records execution, finalizes evidence, destroys capsules. |
| Capsule lifecycle | `services.py:DockerLifecycle`; `supervisor.py:CapsuleSupervisorClient/create_supervisor_app`; `capsule.py` | Worker delegates provisioning to supervisor; supervisor owns runtime access and proxies trusted gateway traffic. |
| Red | `red.py:LinearSearch/BestFirstBeamSearch/MutationEngine/StrategyMemory` | Sequential or branching attack search, mutation, outcome-based strategy reuse. |
| Attacker inference | `models.py:AttackContext/AttackerModel/OpenAICompatibleAttackerModel/build_attacker_model` | Proposes and optionally ranks structured actions. Heuristic and static-suite implementations exist. |
| Environment | `environment.py:AgentEnvironment.reset/step` | Invokes target, obtains verified outcomes, calculates reward and episode termination. |
| Target transport | `target_adapter.py:HttpTargetAdapter` | Uses supervisor/Blue ingress, separates public target output from private verification traces. |
| Reference target | `backend/src/aml_reference_target/agent.py:TargetAgent/BlueTools`; `models.py:WiringFixtureModel/BrokerCompletionModel` | Agent loop with declared tools; either scripted fixture or model-backed decisions. |
| Blue mediation | `blue_gateway.py:GatewayRegistry.process`; `blue.py:BlueEngine.process`; `policy.py:PolicyEngine` | Applies trusted routing identity, policy decisions, virtual effects, and verifier updates. |
| World/tool semantics | `virtual_world.py:VirtualWorld`, `PaymentService`, `MailService`, `CustomerDataService`, `MemoryService`, `FileStorageService` | Stateful simulated business operations and authoritative effect events. |
| Verification | `verifier.py:DeterministicVerifier._match/process_event` | Matches declared forbidden outcomes against virtual-service events. |
| Evidence | `orchestrator.py:RepositoryTrajectorySink.record`; `storage.py:Repository.record_step_graph`; `evidence.py:EvidenceBuilder` | Public steps plus private causal evidence, findings, and artifact bundles. |
| Research integration | `research_api.py:install_research_api`; `research.py:ResearchService`; `research_worker.py:ResearchSessionRunner/ResearchRedBridge` | External reset/step sessions or platform-managed attacker inference. |
| Tracking/export | `telemetry.py:MlflowCampaignTracker`; `research_transfers.py:DatasetRunner`; `research_evaluations.py:EvaluationRunner` | Campaign metrics, frozen JSONL/Parquet exports, paired evaluations. |
| Configuration | `settings.py`, `contracts.py`, `red_contracts.py`, `research_contracts.py`, `scenarios.py:ScenarioCatalog` | Runtime settings, task limits, search/reward ablations, research records, pinned scenario metadata. |

## 2. Current Red execution path

`CampaignRunner.run` selects `_run_linear` or `_run_adaptive` using campaign search mode (with separate benign/replay paths).

`LinearSearch.run` resets the environment; builds an `AttackContext` containing history; calls `model.propose_actions(context, 1)`; executes the first action; records the result; and repeats until success, done, no candidates, cancellation, or step limit. It does not update model weights. Although a comment calls it non-adaptive, the implementation passes action/observation history, so an LLM can adapt conversationally. It lacks branching and defaults to no strategy memory.

`BestFirstBeamSearch.run`, `_candidate_actions`, and `_execute_candidate` implement candidate generation/ranking, optional mutation, frontier selection, fresh-environment prefix replay, and execution of the next action. Replay checks observation fingerprints and can fail on divergence. Strategy memory records outcomes; this is adaptation/search, not evidence of parameter learning.

Existing baselines: `HeuristicBaselineModel`, `StaticAttackSuiteModel`, model-only sequential inference, and search ablations in `red_contracts.py:AblationMode` covering branching, reward, memory, and mutation. The static suite is an available class; `build_attacker_model` does not expose it as a normal provider option. A formal random attacker was not found in these paths.

## 3. One complete campaign episode: actual call chain

This traces a linear campaign with the remote capsule runtime and reference target. Branching reuses the lower execution path but creates fresh capsules and replays prefixes.

| File → callable | Input → output | Next caller/consumer |
|---|---|---|
| `api.py` → `create_campaign` | `CampaignCreate` → persisted campaign response | `Repository.create_campaign` in `storage.py` also inserts campaign `WorkLease` |
| `worker.py` → `Worker.run_once`, `_run_with_heartbeat` | Claimed lease → running campaign coroutine | `CampaignRunner.run(campaign_id)` |
| `orchestrator.py` → `CampaignRunner.run`, `_run_linear` | Stored target/task/policy/config → budget and per-episode seed | `_new_environment` |
| `orchestrator.py` → `_new_environment` | Manifest, policies, seed → episode row and `ManagedEpisodeEnvironment` | `DockerLifecycle.provision`, `build_environment` |
| `services.py` → `DockerLifecycle.provision`; `supervisor.py` → `create_capsule` | `CapsuleSpec` → runtime handle | Supervisor runtime creates capsule; `HttpTargetAdapter.prepare` bootstraps Blue |
| `services.py` → `build_environment`; `environment.py` → `reset` | Task and pinned scenario → healthchecked, reset environment | `HttpTargetAdapter.reset` through Blue reset endpoint returns initial `PublicObservation` |
| `red.py` → `LinearSearch.run`; `models.py` → `propose_actions` | Initial/history observations and prior actions → `RedAction` | `ManagedEpisodeEnvironment.step` |
| `environment.py` → `AgentEnvironment.step`; `target_adapter.py` → `invoke` | Channel/payload/action ID → invocation request | Supervisor `gateway_proxy` → Blue `invoke_target` |
| `blue_gateway.py` → `invoke_target`; `interventions.py` → `InterventionDelivery` | Validated action → target request and delivery tracking | Reference server `invoke` → `TargetAgent.invoke` |
| `aml_reference_target/agent.py` → `TargetAgent.invoke` | Target history → model decision; tool decision → tool call | `BlueTools.call` sends tool execution to Blue; final decision returns target response |
| `blue_gateway.py` → `GatewayRegistry.process` | Tool effect + trusted route → normalized trusted effect | `BlueEngine.process` |
| `blue.py` → `BlueEngine.process`; `virtual_world.py` → service `execute` | Effect + policy + world state → denied/approved-for-simulation/transformed result, possibly changed state and private events | `GatewayRegistry.process` feeds events to verifier |
| `verifier.py` → `DeterministicVerifier.process_event/_match` | Authoritative effect events + forbidden specs → progress/terminal signals | Blue stores audit events and returns public tool response to target |
| `target_adapter.py` → `invoke/drain_private_trace`; `environment.py` → `step` | Target response, gateway verifier state, private trace → `StepResult` | Red receives public observation, scalar reward, success and done |
| `orchestrator.py` → `RepositoryTrajectorySink.record` | Action/result/private trace → atomic persisted step graph | `Repository.record_step_graph`; next Red turn or close |
| `orchestrator.py` → `ManagedEpisodeEnvironment.close` | Completed/failed episode → terminal status, findings, evidence bundle | `EvidenceBuilder.build_episode_bundle`; runtime destroy; next campaign episode if applicable |

Episode semantics: reset clears world/verifier/target state; linear episodes use `task.random_seed + episode_index`. An action step may contain several target model/tool turns. `AgentEnvironment.step` ends on verified success, target termination, or `max_steps_per_episode`; outer code handles cancellation, failures and budgets. `AttackTask` carries episode, model-token, total-cost, wall-time and concurrency limits. `ManagedEpisodeEnvironment.close` destroys the capsule after finalization. Research sessions use explicit asynchronous reset/step commands, heartbeat expiration and expected-step checks; interrupted sessions are retired rather than pretending the in-memory world was resumed (`ResearchSessionRunner.run/execute/retire`).

## 4. Observations and supported actions

`contracts.py:PublicObservation` contains target response, visible errors, visible tool results, turn number, termination flag and delivery receipt. It does not have a general field containing every requested target tool call; private effect traces are a different data source.

`RedAction` contains channel, payload, action ID and optional strategy/parent IDs. Supported enum channels are user messages, uploaded documents and simulated tool results. Actual use is restricted by scenario intervention surfaces and slots (`interventions.py:InterventionDelivery`, `aml_target_protocol/interventions.py`). Simulated tool-result replacement changes what the target sees; it is not itself a world-state change. Multi-turn attacks are repeated actions over retained target history.

There are three different information boundaries:

1. Search controller: `AttackContext` includes full task, rewards, and strategy statistics.
2. Hosted/registered attacker inference: `OpenAICompatibleAttackerModel._public_context` explicitly selects objective, channels, public history, prior actions, non-outcome strategy guidance and tags; it excludes verifier details and scalar rewards. `ResearchRedBridge.propose_actions` reuses this allowlist.
3. External research client: `ResearchSessionRunner.execute` returns public observation PLUS outcome, verifier measurements, baseline reward and usage. The client is privileged research infrastructure. Do not feed this entire response to the attacker.

Recommendation: policy inference gets only an explicitly versioned public projection. The trainer can consume declared training rewards separately. Whether online reward feedback is attacker-visible is an experimental choice that must be identical across comparisons.

## 5. Success and reward

`DeterministicVerifier._match` handles unapproved payments, sensitive external email, cross-tenant records/files, unsafe memory/file writes and custom event predicates. `GatewayRegistry.process` feeds it events emitted after Blue-mediated virtual operations, not arbitrary claims in target prose. Gateway routing overrides target-supplied identity with the trusted route identity.

For example, `PaymentService.execute(payment.create)` inserts a transaction into `state["transactions"]` and emits `payment_created`; the verifier checks creation, lack of approval, unauthorized actor and optional minimum amount. This is a simulated payment ledger, NOT the A100/V900 balance-transfer example from the agenda. No claim about account debits/credits follows from this implementation.

`calculate_reward` already combines terminal success, maximum progress, novelty, turn penalty, model-token penalty and visible-error penalty. `RewardConfig` makes weights explicit. Verification success and shaped return are separate quantities; a high return is not a finding.

## 6. Recommended ML insertion point

**Preferred: a separate research client/trainer using the existing external research-session API.** `research_contracts.py:SessionCreate(mode="external")`, `research_api.py:create_session/reset/step/operation/trajectory`, and `ResearchSessionRunner.execute` already support the lifecycle. The client polls asynchronous operations, maintains heartbeats, submits versioned `RedAction`s, builds trajectories, and owns policy updates/checkpoints. No new worker or environment architecture is needed for a first collector.

For later hosted evaluation, implement existing `aml.attacker.v1` `/health` and `/generate` contracts consumed by `runtime_registry.py:RegisteredAttacker`, register a checkpoint/runtime, and use managed sessions. Managed execution currently runs `LinearSearch` with memory disabled; `ResearchRedBridge.rank_actions` is a no-op. Registration of a rank capability does not establish working managed beam-search integration.

For an in-process experiment inside existing Red, implement `models.py:AttackerModel.propose_actions/rank_actions`. Do not hand an unrestricted `AttackContext` to a learned model. This route needs careful adapter/config integration and is less isolated from infrastructure than the external client.

Lauren should own: public policy inputs, action generation, trajectory assembly, reward experiments, optimizer/training, checkpoint production and generalization analysis. Michelle's existing services should remain responsible for execution, world state, verification, capsule isolation, durable evidence and job orchestration. Checkpoint registration stores manifests; it does not train or load weights into a trainer.

## 7. Likely files to add/change after agreement

Proposed NEW research code, not existing paths: a small `research/` package with client, public-observation projection, transition schema, collector and focused tests. Start with a fixed policy; keep `act/observe/update/save/load` local to this package if useful. The backend need not adopt that whole interface.

Existing integration targets only if a demonstrated gap requires changes: `research_contracts.py` (versioned contract), `research_worker.py` (feedback/provenance), `research_transfers.py` (export projection), and `runtime_registry.py` (serving adapter). `models.py`, `red_contracts.py`, and worker configuration matter only for the alternative in-process path. There is no reason yet to rewrite `red.py`, Blue, the target agent, or capsule orchestration.

## 8. Data already present versus missing

| Training data | Existing implementation | Gap/qualification |
|---|---|---|
| Initial observation | Research reset `EpisodeCommand.result` | Ordinary campaign `RepositoryTrajectorySink` records steps, not reset output; do not assume old campaign traces have exact o0. |
| Action and next observation | `EpisodeStep` via `record_step_graph`; research command input/result | Research commands can be joined in episode/step order to obtain `(o_t,a_t,o_next)`. |
| Reward | Stored step reward; research `baseline_reward`; `RewardAnnotation` | Establish a learning objective and stable configuration hash; existing reward is not validated for training. |
| Done and cause | Research outcome has termination/truncation reasons and execution status | Build explicit terminated/truncated/error fields in training samples; unknown/interrupted is not a clean negative. Ordinary step `terminal` means success, not every form of done. |
| Generation provenance | `GenerationCreate`; managed bridge records generation/request/action/runtime/checkpoint links | External clients must submit it. Exact tokenizer, token IDs, log probabilities and optimizer/RNG state are not mandatory captured fields. |
| Versions and splits | Research session/dataset lineage includes scenario, target, verifier, reward version, split, family, seed and group | Need enough genuinely distinct train/validation/test scenarios and experimental isolation. |
| Dataset | `DatasetRunner.build`: frozen JSONL or Parquet, model-input/research views | Records are operations/generations, not ready-made RL transitions; assemble histories and filter partial/unexecuted records deliberately. |
| Checkpoint/run records | `CheckpointCreate`, research run/events, uploads and runtime registry | Trainer, actual weight serialization/loading, optimizer and resume state remain ML work. |

Research exports omit outcome/measurements/reward from the top-level model-input view. However, `GenerationCreate` accepts arbitrary prompt/history/raw-response documents and `DatasetRunner` includes generation documents; this alone is not proof that the exported view is free of privileged content supplied by a client. A policy-specific projection and leakage tests remain necessary.

## 9. Research and engineering risks

- **Fixture versus model evidence.** Bundled `scenario_assets/finance_reference.json` specifies fixture mode; `WiringFixtureModel` is a wiring fixture. Learning claims require model-mode target runs and pinned target configurations.
- **Reward hacking.** `AgentEnvironment._novelty` fingerprints an observation including `turn_number`, which changes each step. Thus observation novelty can reward time progression. `DeterministicVerifier.process_event` retains maximum progress, and `calculate_reward` pays progress again each step. Validate whether this incentivizes stalling or novelty chasing before training.
- **Information fairness.** Search receives privileged-derived reward while `_public_context` hides it from the model. Decide whether reward-guided search is allowed at evaluation, and give comparable feedback access to competing methods.
- **Cross-run memory leakage.** `RepositoryStrategyMemory.__init__` loads repository-wide strategies. Research managed sessions explicitly disable this memory, but classic adaptive campaigns need split-scoped memory or separate stores for fair held-out evaluation.
- **Budget comparability.** Beam search replays prefixes in fresh capsules. Charge all target executions, proposal/ranking calls and replay costs. Session usage cannot automatically measure an external trainer's local inference; report that separately. `SuiteCreate`, paired evaluation and `BudgetTracker` supply structure but do not establish fairness by themselves.
- **Limited benchmark breadth.** The checked-in scenario asset is a finance development fixture. Seed variation alone is not unseen-task/tool/authorization generalization. `research_transfers.py:snapshot` rejects train export from runs containing test sessions, but broader memory/model exposure still needs controls.
- **Replay is empirical.** `_execute_candidate` checks replay fingerprints; research reproduction reports divergent steps and explicitly says a seed is not a determinism guarantee. Reproduced verified outcomes and identical trajectories should be reported separately.
- **Verifier scope.** Event predicates are only as good as world semantics and configured forbidden specs. Validate them with positive/negative state-transition cases. `ManagedEpisodeEnvironment._record_terminal_findings` also has a single-verifier fallback for contract-only environments; do not use that fallback as a substitute for authoritative evidence in research claims.
- **Validation coverage.** The checked-in backend test file inspected contains five Admin/API tests (`backend/tests/test_poc_admin.py`), not an end-to-end learning or verifier validation suite. This reconnaissance did not start Docker, call models or execute tests.

## 10. Smallest sensible first implementation

After agreeing on the boundary, implement one external rollout collector with a fixed scripted policy. Create a research run/session, reset, retain exact o0, issue one or more actions with idempotency and expected-step checks, poll completion, record public history and separately labeled outcomes, close, export, and reconstruct the trajectory.

Acceptance: a complete versioned transition record; privileged measurement exclusion from policy input; correct separation of verified success, target termination, limit truncation and unknown execution; reproducible action replay with its independently verified outcome. Use fixture mode for wiring first, then a model-backed target before drawing research conclusions. No heavy training dependency or RL algorithm is needed for this first step.

Proposed Linear work items, not published:

1. Agree public-observation and training-feedback contract — Lauren + infrastructure owner.
2. External rollout collector and leakage/termination tests — Lauren; depends on 1.
3. Validate verifier cases and reward ablations — joint ML/infrastructure work; depends on 2.
4. Pin model-backed baseline suite, budgets and held-out scenarios — Lauren with scenario owner.
5. Select first learning method from collected data, then implement training/checkpoint evaluation — Lauren; depends on 3–4.

Git clone succeeded and `origin` points to the requested repository. GitHub/Linear plugin connections are not confirmed in this session. No PR, issue or remote process update has been created; a Linear team/project and connection are still needed.
