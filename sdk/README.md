# AML research SDK

Install with `pip install ./sdk` from the repository root. Python 3.10+ is supported. This independent package imports no backend modules and needs only the research API URL and a bearer token.

Start with the [root fixture runbook](../README.md) to build and register the supplied
finance target. `catalog()` requires at least one registered bundle for the example
below. The project supplies one controlled target with fixture/model modes; see
[current scope and remaining acceptance work](../PROJECT_STATUS.md).

```python
import asyncio
import os
from aml_research import Client

async def main():
    async with Client(os.environ["AML_API_URL"], os.environ["AML_TOKEN"]) as client:
        bundles = await client.catalog()
        async with client.session(bundle_id=bundles[0]["bundle_id"]) as session:
            initial = await session.reset()
            # Supply only initial.public_observation to your attacker.
            result = await session.step({"channel": "user_message", "payload": {"text": "Process invoice-001."}})
            print(result.outcome)

asyncio.run(main())
```

The protocol is `aml.research.v1`; the client sends its version on every request. `EpisodeResult.public_observation` is the only automatic attacker input. Outcomes, measurements, baseline rewards and usage are separate research outputs. Reported training rewards do not modify verified outcomes.

`Client.session()` bounds concurrent sessions and maintains heartbeats. Every reset creates a fresh episode. Steps carry the episode ID and expected index. Retain `session.id` and `session.last_operation_id`; recover a completed request with `client.operation(operation_id)`. `attach_session()` resumes control of a still-live session. `OperationTimeout` exposes the operation ID. `IndeterminateOperation` means an uncertain side effect: never blindly resubmit its action. Worker or supervisor interruption retires the episode.

HTTP retries are bounded and preserve the original JSON and idempotency key. Reusing a key with different content fails. After reconnect, recover an operation rather than recomputing its step index. Failed streamed transfers can be retransmitted with the same upload key; this restarts the whole file. A transfer left active by a process crash must be cancelled or expire before restarting. Failed transfers cannot create checkpoints. Downloads verify SHA-256 before replacing their destination.

`examples/workflows.py` demonstrates episodes, concurrency, export, external training lifecycle, checkpoints, runtime registration and evaluations. Set `AML_API_URL` and `AML_TOKEN`, then use `python sdk/examples/workflows.py episode` from the repository root. Other workflows read their selected immutable IDs from environment variables shown in the source. Runtime registration requires operator scope. `fixture_runtime.py` is explicitly a contract fixture, not a learned attacker.

Your externally launched training script owns dependencies, hardware, training and restoration of optimizer/model state. The backend records lifecycle events, metrics, dataset references, checkpoints and resume lineage.

`examples/acceptance.py` validates a deployed reference fixture through HTTP, including
reset isolation, exact generations, exports, checkpoint round-trip, paired evaluation,
fresh reproduction and the UI proxy. Follow the backend's
[research deployment guide](../backend/docs/michelle_archive/RESEARCH_INTEGRATION.md). Real-model
acceptance is deliberately left as a placeholder until your model configuration exists.
