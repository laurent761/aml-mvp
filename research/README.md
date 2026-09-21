# AML research client

Lauren's external rollout collector, separate from backend deployment. Python 3.9+
and the standard library suffice. This first increment uses a fixed action sequence;
it does not train a model or claim attack success from generated text.

From `research/`, with the existing backend stack running and a target bundle registered:

```bash
curl http://localhost:8000/v1/research-catalog
mkdir -p outputs
PYTHONPATH=src python3 -m aml_research.collector \
  --bundle-id BUNDLE_ID_FROM_CATALOG \
  --code-revision YOUR_GIT_COMMIT \
  --actions examples/actions.json \
  --output outputs/first-rollout.jsonl \
  --seed 0 --max-steps 2 --max-cost 25
```

The output must be a new file. Its parent directory must exist. Set the revision to
the code actually used and note uncommitted changes in experiment records. The CLI
also accepts `--base-url`, `--max-tokens` and `--max-seconds`. The catalog distinguishes
fixture and model execution; fixture runs validate wiring, not real model robustness.
Alternatively install this package and use `aml-collect`.

## Boundary

`client.py` handles HTTP, idempotent transport retries and asynchronous command polling.
`collector.py` owns one session/episode, background heartbeats, generation registration,
cleanup and a flushed JSONL journal. `transitions.py` separates public policy inputs
from outcomes, verifier measurements, baseline reward and usage. `policies/fixed.py`
provides an observation-independent baseline with an explicit finite action sequence.

The policy receives only public observation history. The collector keeps privileged
research fields outside that history. Visible tool results remain public data as
defined by the backend; this projection is not a sanitizer for arbitrary secrets
already exposed by the target. A learned policy will need an explicit public context
contract including objective/action history; that is not implemented yet.

Each transition preserves o_t, action, o_next, reward, termination/truncation and
execution status. `training_eligible` is conservative: errors and indeterminate
execution are excluded. Reward is the backend baseline, not a validated learning
objective. A positive reward does not imply success. Policy exhaustion is a collector
stop event, not a fabricated environment terminal transition. Failed/unknown commands
are retained in the journal without inventing missing observations or negative labels.

## Validation and next steps

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests use a simulated API transport and require no Docker, credentials or model calls.
Run a live fixture rollout next, inspect its journal and server evidence, and request
fresh reproduction through the platform before making reproducibility claims.
Automatic reproduction, dataset-snapshot download, optimizer updates and checkpoint
training are not part of this increment. Run/session/generation IDs in the journal
allow platform-side inspection and dataset exports later. Local policy inference
usage is not metered by the backend; this fixed policy makes no model calls.

Do not use this client for concurrent calls on the same session. It executes one
episode, closes it, and does not resume an interrupted in-memory environment. An
operation timeout leaves execution uncertain and triggers a close request. If cleanup
fails, the journal records it and backend heartbeat expiration is the remaining
cleanup mechanism. HTTP application errors are surfaced rather than automatically
retried with changed inputs.
