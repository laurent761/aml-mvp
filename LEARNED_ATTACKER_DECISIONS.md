# Learned Attacker — Implementation Decision Log

**Project:** AGNTZ Adversarial ML POC
**Repository:** `michelle-ralph/aml-mvp`
**Scope:** First trainable attacker vertical slice
**Status:** Frozen for initial implementation

## 1. Objective

The immediate objective is to prove that the adversarial attacker can **improve through learning** against executable agent environments.

The first implementation must establish the complete ML loop:

**campaign execution → trajectory collection → training → checkpoint → inference → evaluation**

We are not attempting to build the final attacker architecture in this phase.

The purpose of this implementation is to determine whether there is a learnable adversarial signal in the existing environment.

---

## 2. Existing Infrastructure Is Inherited

The current repository already contains substantial infrastructure that should be reused rather than rebuilt.

Relevant existing components include:

* `AttackerModel`
* `AttackContext`
* `RedAction`
* `PublicObservation`
* `StepResult`
* `LinearSearch`
* existing branching/search infrastructure
* `HeuristicBaselineModel`
* `StaticAttackSuiteModel`
* `OpenAICompatibleAttackerModel`
* `RedExperimentConfig`
* reward configuration
* trajectory/evidence persistence
* research sessions
* checkpoint concepts
* evaluation infrastructure
* paired baseline comparison
* deterministic verifier
* executable reference target
* frozen scenario bundles

The learned attacker must integrate with these contracts.

Do not introduce a parallel campaign runner, evaluator, environment abstraction, or experiment framework unless an actual missing capability requires it.

---

## 3. Primary Engineering Decision

Implement a new learned attacker behind the existing `AttackerModel` interface.

Conceptually:

```python
class LearnedAttackerModel:
    async def propose_actions(
        self,
        context: AttackContext,
        count: int,
    ) -> list[RedAction]:
        ...

    async def rank_actions(
        self,
        context: AttackContext,
        actions: list[RedAction],
    ) -> list[RankedAction]:
        ...
```

The rest of the system should not need to know whether the attacker is:

* heuristic,
* static,
* LLM-based,
* or learned.

---

## 4. First Learning Problem

Do **not** begin with a large generative model or full online RL.

The first learned component will be a **small trainable action-ranking policy**.

Given:

```text
state / AttackContext
+
candidate RedAction
```

the model predicts a score representing the expected usefulness of that action.

Conceptually:

```text
score = f(state, candidate_action)
```

The search system can then prefer higher-scoring actions.

This isolates the contribution of learning while preserving the existing candidate generation and execution infrastructure.

---

## 5. State Representation

The attacker may consume only information legitimately available to Red through the existing public interface.

Initial state representation should include:

* attack objective
* available attack channels
* previous Red actions
* public target responses
* visible tool results
* visible errors
* previous rewards
* current trajectory depth / step
* permitted public task metadata

The representation must be deterministic and versioned.

### Critical restriction

The learned attacker must **never receive private verifier state, private evidence, hidden world state, ground truth, or test-only information as model input**.

Verifier information may be used to construct training labels/rewards after execution, but must not leak into inference features.

---

## 6. Training Example Contract

Trajectory execution should be convertible into explicit training examples.

Minimum logical schema:

```text
state
action
reward
next_state
terminal_success
episode_id
scenario_id
scenario_version_id
split
seed
```

Include stable identifiers/version information necessary for reproducibility.

Dataset generation should be deterministic from persisted trajectory artifacts.

The dataset format should be versioned from the beginning.

Example:

```text
aml.learning.dataset.v1
```

---

## 7. Learning Signal

For the first implementation, reuse the existing reward/verifier semantics rather than inventing a second reward system.

Training labels may incorporate:

* terminal forbidden-state success
* verifier progress reward
* existing reward values
* successful vs unsuccessful actions/trajectories

The exact training objective should remain simple and inspectable.

The initial model should answer:

> Given this observable state and these possible actions, which action appears more promising?

---

## 8. Model Complexity

Start with the smallest model capable of demonstrating learning.

Do not introduce a 7B–14B model for the first experiment unless evidence shows it is necessary.

Preferred initial characteristics:

* cheap
* deterministic where possible
* fast to train
* CPU-compatible if practical
* easy to checkpoint
* easy to inspect
* easy to rerun across seeds

Architecture sophistication is secondary to obtaining a valid learning curve.

---

## 9. Proposed Module Boundary

Prefer a dedicated learning package:

```text
backend/src/adversarial_agent_mvp/learning/
    __init__.py
    dataset.py
    features.py
    model.py
    trainer.py
    checkpoint.py
```

Responsibilities:

### `dataset.py`

Convert persisted trajectories into versioned training examples.

Responsible for:

* schema
* validation
* serialization
* deterministic dataset generation
* dataset hashing

### `features.py`

Convert public attack state and candidate actions into deterministic model features.

Must enforce the public/private information boundary.

### `model.py`

Implement the learned policy/ranker and its `AttackerModel` integration.

### `trainer.py`

Training procedure.

Responsible for:

* deterministic seeds
* train configuration
* optimization
* training metrics
* validation metrics

### `checkpoint.py`

Checkpoint save/load contract.

Responsible for:

* model parameters
* feature version
* dataset hash
* training configuration
* seed
* model version
* relevant code/config metadata

---

## 10. Checkpoint Contract

A checkpoint must be sufficient to reproduce inference.

Conceptually:

```text
checkpoint/
    model
    config.json
    training_metrics.json
    dataset_hash
    feature_version
    model_version
```

Loading a checkpoint must not require access to the original training dataset.

A checkpoint must record enough provenance to determine exactly what produced it.

---

## 11. Training Interface

Provide one simple reproducible training entry point.

Conceptually:

```bash
aml train-attacker \
    --dataset <dataset> \
    --seed 42 \
    --output <checkpoint>
```

Exact CLI structure may follow existing repository conventions.

Do not build a separate CLI framework if the repository already provides an appropriate extension point.

---

## 12. First Experiment

The first experiment asks:

> Does attack performance improve as the attacker receives more training experience?

Train checkpoints using increasing amounts of training experience.

Initial target points:

```text
0 episodes
25 episodes
50 episodes
100 episodes
200 episodes
```

These values may be reduced for smoke testing.

Each checkpoint must be evaluated against the same frozen development evaluation cohort.

Primary curve:

```text
training experience → attack success rate
```

Also record:

* verified forbidden-state transitions
* unique verified findings/transitions where available
* steps per success
* token/query budget
* cost per success/finding
* variance across seeds

---

## 13. Baseline for the First Learning Test

The first comparison should isolate the learned ranking contribution.

Compare:

```text
same candidate generation + non-learned ranking
```

against:

```text
same candidate generation + learned ranking
```

Keep the attack budget equal.

This avoids falsely attributing improvements from additional search, tokens, candidates, or execution budget to learning.

Existing heuristic/static/LLM baselines remain important for the later formal POC comparison.

---

## 14. Data Splits

Training and evaluation separation is mandatory.

Use:

```text
TRAIN
DEVELOPMENT / VALIDATION
HELD-OUT TEST
```

During implementation and model iteration:

* train on TRAIN
* tune/inspect on DEVELOPMENT
* do not inspect or optimize against HELD-OUT TEST

Held-out evaluation belongs to the later generalization milestone.

---

## 15. Reproducibility

Every training/evaluation run should preserve:

* dataset version/hash
* model version
* feature version
* experiment configuration
* random seed
* checkpoint identifier
* scenario versions
* attack budget
* evaluation cohort
* resulting metrics

Same configuration + same data + same seed should be reproducible to the practical limits of the selected ML implementation.

---

## 16. Tests Required

Add focused tests around the ML boundary.

At minimum:

```text
test_learning_dataset.py
test_learning_features.py
test_learned_attacker.py
test_training_checkpoint.py
```

Tests should verify:

* deterministic dataset generation
* no private fields enter inference features
* feature stability
* training executes
* checkpoint save/load
* loaded checkpoint produces valid `RedAction` rankings
* existing attacker implementations continue to work
* learned attacker respects existing attack channels/contracts

---

## 17. Definition of Done — First ML Vertical Slice

This implementation is complete when the repository can perform:

```text
execute campaigns
        ↓
persist trajectories
        ↓
build versioned training dataset
        ↓
train learned attacker
        ↓
save checkpoint
        ↓
load checkpoint
        ↓
execute fresh attacks
        ↓
evaluate with existing metrics
```

A single reproducible configuration should be sufficient to run the complete process.

---

## 18. Scientific Success Criterion

Engineering completion is not equivalent to POC success.

The first scientific checkpoint is evidence that:

> Increasing training experience produces measurable improvement in attack performance on a frozen development evaluation cohort under an equal attack budget.

A negative or flat result is still valid experimental evidence.

Do not modify evaluation methodology merely to obtain a positive result.

---

## 19. Explicitly Out of Scope

For this implementation, do not add:

* large attacker models
* full RL infrastructure
* production UI work
* new orchestration architecture
* new environment framework
* cross-model transfer
* cross-agent transfer
* production deployment
* complicated distributed training
* extensive hyperparameter optimization
* held-out test optimization

These are later decisions conditioned on evidence from the first learning experiment.

---

## 20. Guiding Principle

Prefer the smallest implementation that can answer:

> **Can this attacker actually learn to become better at discovering verified forbidden state transitions?**

Architecture exists to answer that experiment—not the other way around.
