"""Exercise the independent SDK's retry and concurrent session contracts."""

import asyncio
import importlib
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "sdk/src"))
    return importlib.import_module("aml_research.client")


async def test_retry_preserves_json_when_callers_mutate_nested_input(sdk):
    payload = {"configuration": {"seed": 1}}
    sent = []

    def respond(request):
        sent.append((request.headers["idempotency-key"], request.content))
        if len(sent) == 1:
            payload["configuration"]["seed"] = 2
            return httpx.Response(503)
        return httpx.Response(201, json={"id": "run_1"})

    async with sdk.Client("http://api", "token", transport=httpx.MockTransport(respond)) as client:
        await client.request("POST", "/v1/research-runs", payload, key="stable")

    assert len(sent) == 2
    assert sent[0] == sent[1]
    assert json.loads(sent[1][1])["configuration"]["seed"] == 1


@pytest.mark.parametrize("body", [[], None, "upstream failure"])
async def test_non_object_json_errors_preserve_api_exception(sdk, body):
    async with sdk.Client("http://api", "token", retries=0,
            transport=httpx.MockTransport(lambda _: httpx.Response(
                503, content=json.dumps(body), headers={"Content-Type": "application/json"}))) as client:
        with pytest.raises(sdk.ResearchAPIError) as error:
            await client.catalog()
    assert error.value.status == 503


async def test_delayed_heartbeat_cannot_replace_newer_episode_state(sdk):
    heartbeat_started = asyncio.Event()
    release_heartbeat = asyncio.Event()
    original = {"api_version": "aml.research.v1", "id": "session_1", "state": "active",
        "episode_id": "episode_old", "step_index": 4, "configuration": {}}
    latest = {**original, "episode_id": "episode_new", "step_index": 0}

    async def respond(request):
        if request.url.path.endswith("/heartbeat"):
            heartbeat_started.set()
            await release_heartbeat.wait()
            return httpx.Response(200, json=original)
        return httpx.Response(200, json=latest)

    async with sdk.Client("http://api", "token", transport=httpx.MockTransport(respond)) as client:
        episode = sdk.EpisodeSession(client, sdk.SessionInfo.model_validate(original))
        heartbeat = asyncio.create_task(episode.heartbeat())
        await asyncio.wait_for(heartbeat_started.wait(), 1)
        status = asyncio.create_task(episode.status())
        await asyncio.sleep(0)
        release_heartbeat.set()
        await asyncio.wait_for(asyncio.gather(heartbeat, status), 1)
        assert episode.info.episode_id == "episode_new"
        assert episode.info.step_index == 0
