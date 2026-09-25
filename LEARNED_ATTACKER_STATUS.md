# Learned attacker — implementation status

Updated: 2026-09-25
Branch: `lauren/research-rollout-collector`
Push destination: `lauren` (`laurent761/aml-mvp`)

## Achieved and verified

The local vertical slice executes the complete chain:

**finance target + Blue verifier → persisted trajectories → deterministic dataset →
CPU training → checkpoint → reload → fresh attacks → paired evaluation**.

- Implemented a 512-parameter action ranker behind the existing `AttackerModel` protocol.
- Reused campaign/episode storage, the reference target, Blue, Red search, research
  checkpoint manifests, runtime protocol, and evaluation metrics.
- Persisted exact reset observations in existing operational events, allowing complete
  public histories to be reconstructed without inventing missing observations.
- Added versioned datasets, deterministic hashing, seeded training, safe JSON checkpoint
  loading, source/configuration provenance, and checkpoint identity verification.
- Enforced public-only inference features and TRAIN/DEVELOPMENT separation. TEST is
  prohibited in learning datasets. No held-out optimization was performed.
- Integrated the learned provider into model configuration and worker settings, and
  added an `aml.attacker.v1` runtime for managed research evaluation.
- Added the `aml` CLI and a reproducible local smoke configuration.

Validation completed:

| Check | Result |
| --- | --- |
| Backend test suite | 14 tests passed, including the original five tests |
| Ruff | Passed on backend source/tests and the Docker acceptance driver |
| Pyright | Backend passed with zero errors/warnings; driver checked separately |
| Dataset reconstruction via installed CLI | Byte-identical to the persisted dataset export |
| Training via installed CLI | Completed and saved a loadable checkpoint |
| Registered runtime HTTP protocol | Passed using the existing `RegisteredAttacker` client |
| Local executable smoke | Four TRAIN episodes, two fresh DEVELOPMENT episodes per policy |
| Equal evaluation budget | Two executed target queries for each policy |
| Smoke attack success | 0% for learned ranking and 0% for fixed ranking |

The smoke verifies engineering integration. Its scripted finance cohorts are not
independent generalization tasks, and the result does not establish improved attack
performance through learning. The focused training test demonstrates reward fitting
on a tiny controlled fixture; it is not a scientific attack-performance result.

Local smoke artifacts are under `backend/var/learning-smoke-final/`. They are ignored
runtime output and are not part of the commit. The checked-in configuration, tests,
and runbook reproduce the experiment.

## Docker work completed

- Confirmed Docker was initially reachable (server version 29.6.1).
- Attempted to build/start the API, worker and their service dependencies.
- Diagnosed failed pulls for the pinned MinIO Docker Hub images. Successfully pulled
  the same release versions from Quay and updated Compose to use those locations.
- Added Compose forwarding for learned-checkpoint worker settings and normalized empty
  optional checkpoint environment values.
- Changed dependency download caches to BuildKit cache mounts in the backend and
  reference-target Dockerfiles so those caches are not embedded in image layers.
- Added `backend/scripts/docker_learning_smoke.py`. It uses the existing research
  client/API, trains inside the deployed API container, registers a checkpoint, starts
  temporary attacker runtime containers, and requests a managed evaluation pair.
  It checks clean execution and containment evidence before reporting success.

## Docker end-to-end verification — passed 2026-09-25

The stack rebuilt successfully, migrations completed, and service health checks passed.
The finance reference target was built and preserved in the local registry. The live
acceptance driver completed collection → dataset → training → checkpoint registration
and reload → managed paired evaluation using the scripted finance fixture.

| Check | Result |
| --- | --- |
| TRAIN collection | Two episodes, seeds 42 and 43; two exported examples |
| CPU training | Completed inside the deployed API container |
| Checkpoint | Registered, loaded locally, and served by temporary Docker runtimes |
| Managed DEVELOPMENT evaluation | One fresh episode per policy, seed 100 |
| Equal execution budget | One executed target query per policy |
| Target execution | Both evaluation operations returned `ok`; no infrastructure failures |
| Containment evidence | Both capsules `VERIFIED`, internal networks, zero violations |
| Attack success | Fixed ranking 0%; learned ranking 0% |
| Runtime cleanup | Temporary attacker containers removed by the driver |

This verifies deployment integration, not improved attack performance or generalization.
The backend stack remains running. The UI was tested separately afterward; see
[UI smoke-test results and follow-ups](UI_TEST_STATUS.md).

Persisted identifiers:

- Run: `run_3307ef11bc8f4562b3b1b26c59ba8ad2`
- TRAIN campaigns: `campaign_1b24309ebb3c4bad85b080b66b185570`,
  `campaign_28608ff3005044799bd96d8321972d17`
- Checkpoint: `checkpoint_4a216f975a6648f48b00bc8e5d10b56b`
- Evaluation: `evaluation_1aaca2ebc29a422ea444e4597912fa75`
- Dataset hash: `522437c88316186457d297de4e62797390bc889007878b4c9d09100341eb54c5`
- Checkpoint hash: `615b21b85b2da973d54a5bdc56af9804beeac23c16570e143f11ceeec0e1d9ab`

Local artifacts (ignored runtime output):

- `backend/var/docker-learning/run-1/report.json`
- `backend/var/docker-learning/run-1/train.jsonl`
- `backend/var/docker-learning/run-1/checkpoint.json`
- `backend/var/bundles/docker-reference.json`

## Disk-space blocker — resolved

The previous attempt failed during image extraction when the host ran out of space.
On September 25, approved unused Docker build-cache cleanup reported 10.37 GB cleared,
raising host free space from 7.7 GiB to 17 GiB. Images, containers and data volumes were
preserved. Docker responded normally and the subsequent build and acceptance run passed.
After rebuilding and running the test, approximately 6.3 GiB remained free.

## Repeat the Docker acceptance run

From `backend/`, with Docker available:

```bash
docker compose up -d --build --wait api worker target-registry
uv run adversarial-bundle build-reference --output var/bundles/docker-reference.json
uv run python scripts/docker_learning_smoke.py \
  --bundle var/bundles/docker-reference.json --output var/docker-learning/run-2
```

Use a new output directory for every run. The driver preserves the report, dataset,
and checkpoint and removes its temporary attacker runtime containers.

## Next experiment

The next scientific experiment is the frozen-development
learning curve at 0/25/50/100/200 TRAIN episodes across seeds 7/42/123, using the same
candidate pool and attack budget. A flat or negative result remains valid evidence.

See [the runbook](LEARNED_ATTACKER.md) for architecture, training and evaluation commands,
and [the supplied decision log](LEARNED_ATTACKER_DECISIONS.md) for the implementation scope.
