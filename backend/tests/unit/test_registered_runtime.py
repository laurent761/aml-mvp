import httpx
import pytest

from adversarial_agent_mvp.runtime_registry import RegisteredAttacker


async def test_failed_raw_response_and_input_rejection_do_not_reuse_previous_usage():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, text="malformed generation payload")
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    runtime = RegisteredAttacker({"name": "fixture", "version": "1", "model": "fixture",
        "checkpoint_id": "checkpoint-fixture", "endpoint": "http://fixture", "max_input_bytes": 1024}, client=client)
    try:
        with pytest.raises(ValueError):
            await runtime.propose({"observations": []}, 1)
        assert runtime.last_generation is not None
        assert runtime.last_generation["raw_response"] == "malformed generation payload"
        assert runtime.last_usage is not None
        assert runtime.last_usage["tokens"] > 0
        with pytest.raises(RuntimeError, match="input exceeded"):
            await runtime.propose({"observations": [{"text": "x" * 3000}]}, 2)
        assert len(calls) == 1
        assert runtime.last_usage is None
        assert runtime.last_generation is not None
        assert runtime.last_generation["raw_response"] is None
        assert runtime.last_generation["seed"] == 2
    finally:
        await client.aclose()
