# Learned attacker: first vertical slice

The implementation is governed by [LEARNED_ATTACKER_DECISIONS.md](LEARNED_ATTACKER_DECISIONS.md).
It trains a 512-parameter CPU linear reward predictor behind `AttackerModel`.
No additional ML dependencies are required. Training minimizes squared error against
**the existing stored step reward**, with seeded SGD and L2 regularization. The sigmoid
used for ranking maps predictions into `RankedAction`'s required interval; it is not
a calibrated success probability.

## Reproduce the complete engineering experiment

From `backend/`:

```bash
uv sync --extra dev
uv run aml smoke --config experiments/learning-smoke.json \
  --output var/learning-smoke --code-revision working-tree
```

Use an empty output directory. The configuration drives collection, training,
checkpoint reload, and fresh paired evaluation. The run preserves `trajectories.db`,
`train.jsonl`, `checkpoint.json`, `zero.json`, `config.json`, and `evaluation.json`.
The database contains normal campaigns, episodes, steps, operational events and
private verifier evidence. It is not a second trajectory store. Dataset export is
repeatable from that database, including after the training process exits.

The smoke composes existing `open_bundle`, `ManagedEpisodeEnvironment`, `LinearSearch`,
`RepositoryTrajectorySink`, and `run_evaluation`. It runs the real reference target,
Blue tool mediation, virtual services and deterministic verifier through in-process
HTTP. It does not verify Docker containment. Its named TRAIN/DEVELOPMENT cohorts use
the same scripted finance fixture; they are **not independent generalization tasks**.
Candidate prompts come from `HeuristicBaselineModel`; no ground-truth attack is
copied into the ranker's input or candidate pool. Zero success is an expected valid
result for this candidate set. Fixture results do not establish model robustness.

## Build a dataset and train

Execute fresh campaigns through the existing platform using frozen TRAIN scenarios.
Use linear search without strategy memory for the first experiment. Existing research
sessions also persist campaign steps and now receive the same reset event. Do not mix
held-out or globally learned strategy-memory data into the collection cohort.

```bash
uv run aml dataset --campaign CAMPAIGN_ID --campaign ANOTHER_CAMPAIGN_ID \
  --output var/train.jsonl
uv run aml train-attacker --dataset var/train.jsonl --seed 42 \
  --code-revision YOUR_COMMIT --output var/checkpoint.json
```

The dataset command uses the existing backend `DATABASE_URL`. For the smoke database:

```bash
DATABASE_URL=sqlite:///var/learning-smoke/trajectories.db uv run aml dataset \
  --campaign ID_FROM_EVALUATION_JSON --output var/rebuilt.jsonl
```

To measure development loss, export separate development campaigns and pass
`--validation var/development.jsonl`. Training accepts only TRAIN; validation accepts
DEVELOPMENT or its existing `validation` alias. Episode, scenario and scenario-version
identity overlap is rejected. TEST cannot enter the learning dataset schema.

Optional `--config` accepts `TrainConfig` JSON. Defaults are 100 epochs, learning rate
0.1, L2 0.001, and three candidates. An optional `candidates` list of `{channel, payload}`
objects selects the existing `StaticAttackSuiteModel` instead of the heuristic generator.
Freeze that list before evaluation and reuse it for both policies. Do not derive it
from development/test ground truth. `--episodes N` selects nested episode cohorts
ordered by seed and episode ID; `--episodes 0` explicitly creates untrained weights.

A checkpoint is a checksummed JSON weight artifact with strict versions, feature
dimensions, seed, configuration, dataset hash, scenario versions, metrics, Python
version, and a SHA256 of the backend Python source, including uncommitted changes.
Loading requires no dataset and executes no pickle or arbitrary checkpoint code.
Keep the source revision and locked environment with your experiment artifacts.

## Existing campaign integration

Worker settings now accept:

```dotenv
ATTACKER_MODEL_PROVIDER=learned
ATTACKER_CHECKPOINT_PATH=/absolute/path/checkpoint.json
ATTACKER_CHECKPOINT_SHA256=HASH_PRINTED_BY_TRAINING
ATTACKER_LEARNED_RANKING=true
```

The campaign's existing `RedExperimentConfig.model` must match those settings:

```json
{
  "provider": "learned",
  "checkpoint_path": "/absolute/path/checkpoint.json",
  "checkpoint_sha256": "HASH_PRINTED_BY_TRAINING",
  "learned_ranking": true
}
```

Set the existing experiment ablation to `model_only` and search mode to `linear` for
the initial comparison. Set `learned_ranking` false in both worker and experiment for
the fixed-order comparison. Both wrappers request the same configured candidate pool
and execute the same maximum number of actions. The original heuristic, static and
OpenAI-compatible adapters retain their behavior. Local ranking uses zero model tokens;
CPU time is not converted into monetary cost. Do not compare its token accounting
against a bare heuristic adapter's simulated token estimates.

## Existing managed research evaluation

The API already supplies checkpoint upload/registration, runtime registration, frozen
benchmark suites and paired evaluation. The new runtime speaks its `aml.attacker.v1`
protocol. Register the JSON file through those existing routes:

1. `POST /v1/research-runs` with `name` and `code_revision`.
2. `POST /v1/artifact-uploads` with `name`, file `size_bytes`, file SHA256 and `run_id`;
   `PUT /v1/artifact-uploads/UPLOAD_ID/data` with the exact checkpoint file bytes;
   `POST /v1/artifact-uploads/UPLOAD_ID/complete` returns `artifact_id`.
3. Generate the existing checkpoint manifest and register it:

```bash
uv run aml manifest --checkpoint var/checkpoint.json --run-id RUN_ID \
  --artifact-id ARTIFACT_ID > var/checkpoint-manifest.json
curl -fsS http://localhost:8000/v1/checkpoints \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: learned-checkpoint-v1' \
  --data-binary @var/checkpoint-manifest.json
```

Use the returned registered checkpoint ID to start the runtime. Run these in separate
terminals (same weights and candidates, different ranking):

```bash
uv run aml serve --checkpoint var/checkpoint.json --checkpoint-id CHECKPOINT_ID \
  --host 0.0.0.0 --port 8090 --fixed-ranking
uv run aml serve --checkpoint var/checkpoint.json --checkpoint-id CHECKPOINT_ID \
  --host 0.0.0.0 --port 8091
```

Register each with `POST /v1/model-runtimes` using a unique `name` and `version`,
`model: "linear-reward-v1"`, the same `checkpoint_id`, `capabilities: ["propose"]`,
and the respective endpoint reachable **from the worker** (for Docker Desktop,
`http://host.docker.internal:8090` and `:8091`). Confirm each through
`POST /v1/model-runtimes/RUNTIME_ID/health`.

Create a frozen development suite through `POST /v1/benchmark-suites` using `name`,
`version`, development `bundle_ids`, fixed `seeds`, and identical `limits`, including
`max_episodes: 1`. Preserve the suite ID for every checkpoint. Then evaluate:

```bash
curl -fsS http://localhost:8000/v1/evaluations \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: learned-development-v1' \
  -d '{"suite_id":"SUITE_ID","baseline_checkpoint_id":"CHECKPOINT_ID","candidate_checkpoint_id":"CHECKPOINT_ID","baseline_runtime_id":"BASELINE_RUNTIME_ID","candidate_runtime_id":"LEARNED_RUNTIME_ID","mode":"managed"}'
curl -fsS http://localhost:8000/v1/evaluations/EVALUATION_ID
```

Creation routes that accept idempotency keys require a unique key for a new request.
The API, worker, registered target and runtime endpoints must be running for this
managed path. The local smoke and protocol integration test require no services.
Research checkpoint registration continues to report `load_validation: not_asserted`;
only the new loader/runtime actually validate these weights. No evaluator semantics
were changed.

## Boundaries and limitations

- Features explicitly select objective, allowed channels, public observation history,
  previous action contents and depth. They omit private task/verifier fields, evidence,
  scenario IDs, arbitrary metadata, strategy records, reward feedback, and delivery IDs.
  Rewards are labels only, matching the hosted attacker's stricter inference boundary.
  Target-visible tool results are public by contract; this cannot undo a secret already
  exposed by a target response.
- New reset observations are stored as `EPISODE_PUBLIC_RESET` operational events.
  Old campaigns without exactly one reset fail explicitly. No invented initial state
  or migration is used. Failed/ambiguous steps and unfinished episodes are rejected.
- This is immediate-reward regression, not RL or counterfactual supervision. Dataset
  quality, candidate coverage, observation novelty shaping and repeated progress
  rewards may dominate the outcome. Reward semantics remain unchanged.
- Static candidate generation retains the inherited positional slicing semantics.
  The initial one-step comparison avoids multi-turn candidate reuse questions.
- Source artifacts may contain private evidence; only projected dataset state reaches
  inference. Training hashes and provenance are not inference features.
- Dataset generation is deterministic for the same persisted records. Fresh target
  execution can generate different IDs; equal seeds do not promise byte-identical
  campaign artifacts. Repeated training on identical data/config/seed is deterministic.
- Serving uses the existing operator-controlled runtime trust model. The wrapper does
  not support an LLM candidate generator in this first slice. Memory and adaptive
  search should remain disabled when isolating ranking effects.

## Next learning-curve experiment

Freeze a TRAIN collection cohort and a distinct DEVELOPMENT suite with a model-backed
target and pinned target configuration. Freeze an operator-authored static candidate
pool based only on TRAIN experience, with varied outcomes. Collect 200 TRAIN episodes
with randomized candidate choice and one action per episode, preserving every budget
and result. Avoid test scenarios entirely.

For each seed **7, 42, 123**, train fresh checkpoints at **0, 25, 50, 100, 200** episodes:

```bash
uv run aml train-attacker --dataset var/train.jsonl --config var/train-config.json \
  --seed 42 --episodes 25 --code-revision YOUR_COMMIT --output var/seed42-n25.json
```

Register/serve each checkpoint and run the same frozen development suite against the
fixed-order wrapper with identical candidates and limits. Plot training episodes vs
attack success rate. Preserve per-seed values, paired confidence intervals, verified
findings/transitions, steps to success, query/token usage and reported cost. Report
CPU cost as unpriced unless measured externally. Accept a flat or negative curve;
do not change the cohort, metrics or budgets after examining results.

## Files and validation

Added: this guide, the supplied decision log, `backend/experiments/learning-smoke.json`,
the learning package (`__init__`, `dataset`, `features`, `model`, `trainer`, `checkpoint`,
`cli`, `runtime`, `smoke`), four focused test modules and `learning_fixtures.py`.
Modified: README, backend `pyproject.toml`, `models.py`, `red_contracts.py`, `settings.py`,
`worker.py`, `orchestrator.py`, and `bundle_smoke.py`. No database migration or new
runtime dependency.

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
uv run pyright
```

Tests cover deterministic persisted-data reconstruction, hashes, leakage, feature
stability, split rejection, actual reward fitting, zero-experience weights, malformed
checkpoints, checkpoint identity, attacker compatibility, channel enforcement,
registered-runtime HTTP compatibility and the complete executable smoke chain.

Validated locally: **14 backend tests passed**, Ruff passed, and Pyright reported
**0 errors / 0 warnings**. The installed `aml` command rebuilt the saved dataset
byte-for-byte and trained a checkpoint from a two-episode subset. The default smoke
collected four TRAIN episodes and evaluated two fresh DEVELOPMENT episodes per policy;
both policies executed two target queries and reached **0% ASR**. Its artifacts are
in `backend/var/learning-smoke-final/` (ignored runtime output). The managed HTTP protocol
was exercised with the existing `RegisteredAttacker`; a live Docker research evaluation
has not been run.

## Live Docker acceptance driver

After the stack and reference target image are built, the deployment check can be
repeated from `backend/`:

```bash
docker compose up -d --build --wait api worker target-registry
uv run adversarial-bundle build-reference --output var/bundles/docker-reference.json
uv run python scripts/docker_learning_smoke.py \
  --bundle var/bundles/docker-reference.json --output var/docker-learning/run-1
```

The driver uses the existing research client/API and Docker worker to collect two
one-step TRAIN episodes, exports and trains inside the API container, uploads the
checkpoint through the artifact API, registers it, starts temporary baseline/candidate
runtime containers, and requests one managed DEVELOPMENT evaluation pair. It checks
execution status, one query per policy, and persisted containment evidence before
reporting completion. Runtime containers are removed afterward; persisted records,
checkpoint and the local report are retained. The output directory must be new.
This driver has passed syntax/type/lint checks but has not yet completed a live run.

The initial Docker attempt was blocked during image extraction by exhausted host disk
space (approximately 436 MiB free), followed by Docker daemon EOF errors. The pinned
MinIO Docker Hub pulls failed; the exact same releases were successfully pulled from
Quay, and Compose now references those registry locations. Dependency download caches
now use BuildKit mounts instead of being included in backend/target image layers.
The live result remains **unverified** until sufficient disk space is available and
the acceptance driver completes. Backend tests still pass (14/14).
