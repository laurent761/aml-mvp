# Learned attacker — implementation status

Updated: 2026-09-23
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

The driver passed static checks, but its live execution has not been verified.
The final Dockerfile/cache changes have not completed a successful image build yet.

## Current blocker

The host ran out of disk space during image extraction. The latest check showed
approximately **431 MiB free** on the Mac. Build output reported `no space left on
device` and image extraction I/O errors. Docker subsequently returned `EOF`, including
for read-only disk-usage and service-status queries.

**No live Docker campaign → training → managed evaluation run has completed.**
This is an environment blocker, not a passing deployment result. No existing Docker
images, volumes or other-project caches were pruned. Cache cleanup approval remains
pending; the later commit/push request does not authorize that cleanup.

## Resume after freeing space

1. Free at least 5–10 GB of host disk space. Recover/restart Docker Desktop if necessary
   and confirm `docker info` succeeds. Do not delete project data volumes.
2. From `backend/`, rebuild/start the stack:

   ```bash
   docker compose up -d --build --wait api worker target-registry
   ```

3. Build and preserve the reference target:

   ```bash
   uv run adversarial-bundle build-reference --output var/bundles/docker-reference.json
   ```

4. Run the live acceptance driver with a new output directory:

   ```bash
   uv run python scripts/docker_learning_smoke.py \
     --bundle var/bundles/docker-reference.json --output var/docker-learning/run-1
   ```

5. Require a completed report with two collected TRAIN episodes, a registered/reloaded
   checkpoint, one clean managed DEVELOPMENT trial per policy, equal one-step budgets,
   and persisted Docker containment evidence. Record the campaign/checkpoint/evaluation
   IDs and the actual metrics. Fix and document any deployment failures encountered.

After deployment verification, the next scientific experiment is the frozen-development
learning curve at 0/25/50/100/200 TRAIN episodes across seeds 7/42/123, using the same
candidate pool and attack budget. A flat or negative result remains valid evidence.

See [the runbook](LEARNED_ATTACKER.md) for architecture, training and evaluation commands,
and [the supplied decision log](LEARNED_ATTACKER_DECISIONS.md) for the implementation scope.
