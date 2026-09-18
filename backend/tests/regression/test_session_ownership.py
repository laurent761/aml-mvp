"""Session admission and command authorization must not alter another owner's work."""

import pytest
from sqlalchemy import func, select

from adversarial_agent_mvp.bundle_cli import reference_bundle
from adversarial_agent_mvp.research_storage import EpisodeCommand, ResearchOwner, ResearchSession
from adversarial_agent_mvp.scenarios import ScenarioCatalog
from adversarial_agent_mvp.storage import Campaign, WorkLease

pytestmark = pytest.mark.integration


@pytest.fixture
async def queued_session(research_api):
    registered = ScenarioCatalog(research_api["repository"]).register(reference_bundle("sha256:" + "a" * 64))
    body = {"bundle_id": registered.bundle_id, "limits": {"max_cost": 7}}
    created = await research_api["alice"].post("/v1/research-sessions", json=body,
        headers={"Idempotency-Key": "session"})
    assert created.status_code == 202, created.text
    session = created.json()
    pending = await research_api["alice"].post(f'/v1/research-sessions/{session["id"]}/reset', json={},
        headers={"Idempotency-Key": "reset"})
    assert pending.status_code == 202, pending.text
    return session, pending.json(), body


@pytest.mark.parametrize("method,route", [
    ("GET", "detail"), ("GET", "trajectory"), ("GET", "operation"),
    ("POST", "heartbeat"), ("POST", "reset"), ("POST", "steps"),
    ("POST", "close"), ("POST", "cancel"),
])
async def test_foreign_owner_cannot_read_replay_or_mutate_pending_work(research_api, queued_session, method, route):
    session, pending, _ = queued_session
    prefix = f'/v1/research-sessions/{session["id"]}'
    path = {"detail": prefix, "operation": f'/v1/research-operations/{pending["id"]}'}.get(route, f"{prefix}/{route}")
    body = {"episode_id": "unknown", "expected_step_index": 1,
            "action": {"channel": "user_message", "payload": {"text": "unauthorized"}}} if route == "steps" else {}
    before = (await research_api["alice"].get(prefix)).json()
    denied = await research_api["bob"].request(method, path, json=body if method == "POST" else None,
        headers={"Idempotency-Key": "reset"})
    assert denied.status_code == 404, denied.text
    assert session["id"] not in denied.text
    assert pending["id"] not in denied.text
    assert (await research_api["alice"].get(prefix)).json() == before
    trajectory = (await research_api["alice"].get(f"{prefix}/trajectory")).json()["items"]
    assert [entry["id"] for entry in trajectory] == [pending["id"]]
    assert trajectory[0]["status"] == "pending"
    assert (await research_api["bob"].get("/v1/research-sessions")).json() == {"items": []}


async def test_session_and_command_retries_do_not_reserve_cost_or_enqueue_twice(research_api, queued_session):
    session, pending, body = queued_session
    client = research_api["alice"]
    current = (await client.get(f'/v1/research-sessions/{session["id"]}')).json()
    replay = await client.post("/v1/research-sessions", json=body, headers={"Idempotency-Key": "session"})
    assert replay.status_code == 202
    # Resource retries return current state, including the heartbeat advanced by reset submission.
    assert replay.json() == {key: value for key, value in current.items() if key != "usage"}
    reset = await client.post(f'/v1/research-sessions/{session["id"]}/reset', json={},
        headers={"Idempotency-Key": "reset"})
    assert reset.status_code == 202
    assert reset.json() == pending
    with research_api["repository"].db.session() as db:
        for table in [Campaign, WorkLease, ResearchSession, EpisodeCommand]:
            assert db.scalar(select(func.count()).select_from(table)) == 1
        owner = db.get(ResearchOwner, "alice")
        assert owner.reserved_cost == 7
        assert owner.spent_cost == 0


@pytest.mark.parametrize("route,payload,key", [
    ("reset", {"seed": 99}, "reset"),
    ("reset", {}, "another-reset"),
    ("close", {}, "reset"),
    ("cancel", {}, "reset"),
])
async def test_conflicting_or_overlapping_commands_leave_queue_and_stop_reason_unchanged(
    research_api, queued_session, route, payload, key,
):
    session, pending, _ = queued_session
    client = research_api["alice"]
    path = f'/v1/research-sessions/{session["id"]}'
    before = (await client.get(path)).json()
    conflict = await client.post(f"{path}/{route}", json=payload, headers={"Idempotency-Key": key})
    assert conflict.status_code == 409
    assert (await client.get(path)).json() == before
    operations = (await client.get(f"{path}/trajectory")).json()["items"]
    assert [operation["id"] for operation in operations] == [pending["id"]]
    assert operations[0]["status"] == "pending"
