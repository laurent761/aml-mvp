"""Durable idempotency, immutable history, and owner isolation through the API."""

import asyncio

import httpx
import pytest
from sqlalchemy import select

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.research_storage import ResearchRecord
from adversarial_agent_mvp.storage import Database

pytestmark = pytest.mark.integration

RUN = {"name": "regression-run", "code_revision": "immutable-revision",
       "configuration": {"optimizer": {"rate": 0.01, "seed": 7}, "labels": ["a", "b"]}}


async def create_run(client, *, key="run", body=None):
    response = await client.post("/v1/research-runs", json=RUN if body is None else body,
        headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()


async def test_run_retry_survives_fresh_database_connection_and_application(research_api):
    original = await create_run(research_api["alice"])
    reopened = Database(research_api["settings"].database_url)
    try:
        app = create_app(research_api["settings"], database=reopened, create_schema=False,
            artifact_store=research_api["store"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://restarted",
                headers={"Authorization": "Bearer alice"}) as client:
            reordered = {"configuration": {"labels": ["a", "b"], "optimizer": {"seed": 7, "rate": 0.01}},
                         "code_revision": RUN["code_revision"], "name": RUN["name"]}
            assert await create_run(client, body=reordered) == original
            conflict = await client.post("/v1/research-runs", json={**RUN, "code_revision": "different"},
                headers={"Idempotency-Key": "run"})
            assert conflict.status_code == 409
            listing = (await client.get("/v1/research-runs")).json()["items"]
            assert listing == [original]
    finally:
        reopened.engine.dispose()


async def test_identical_idempotency_keys_are_independent_between_owners(research_api):
    alice = await create_run(research_api["alice"])
    bob = await create_run(research_api["bob"], body={**RUN, "name": "bob-only"})
    assert alice["id"] != bob["id"]
    for owner, expected in [("alice", alice), ("bob", bob)]:
        assert (await research_api[owner].get("/v1/research-runs")).json()["items"] == [expected]
        other = bob if owner == "alice" else alice
        denied = await research_api[owner].get(f'/v1/research-runs/{other["id"]}')
        missing = await research_api[owner].get("/v1/research-runs/nonexistent")
        assert denied.status_code == missing.status_code == 404
        assert denied.json() == missing.json()


async def test_concurrent_identical_retries_create_exactly_one_record(research_api):
    client = research_api["alice"]
    records = await asyncio.gather(*(create_run(client) for _ in range(8)))
    assert all(record == records[0] for record in records)
    assert (await client.get("/v1/research-runs")).json()["items"] == [records[0]]
    with research_api["repository"].db.session() as db:
        assert list(db.scalars(select(ResearchRecord.id))) == [records[0]["id"]]


async def test_run_pagination_filters_ownership_before_applying_offset(research_api):
    alice = research_api["alice"]
    oldest = await create_run(alice, key="oldest", body={**RUN, "name": "oldest"})
    middle = await create_run(alice, key="middle", body={**RUN, "name": "middle"})
    newest = await create_run(alice, key="newest", body={**RUN, "name": "newest"})
    await create_run(research_api["bob"], body={**RUN, "name": "foreign-newest"})
    for offset, expected in [(0, [newest]), (1, [middle]), (2, [oldest]), (3, [])]:
        response = await alice.get(f"/v1/research-runs?limit=1&offset={offset}")
        assert response.status_code == 200
        assert response.json()["items"] == expected


@pytest.mark.parametrize("key", [None, "", "x" * 201, b"non-ascii-\xe9"])
async def test_invalid_idempotency_keys_leave_no_durable_record(research_api, key):
    response = await research_api["alice"].post("/v1/research-runs", json=RUN,
        headers={} if key is None else {"Idempotency-Key": key})
    assert response.status_code == 422
    assert (await research_api["alice"].get("/v1/research-runs")).json() == {"items": []}


@pytest.mark.parametrize("key", ["x", "x" * 200])
async def test_idempotency_key_length_boundaries_round_trip(research_api, key):
    record = await create_run(research_api["alice"], key=key)
    assert await create_run(research_api["alice"], key=key) == record
    assert (await research_api["alice"].get("/v1/research-runs")).json()["items"] == [record]


async def test_events_are_idempotent_per_run_and_conflicts_preserve_history(research_api):
    client = research_api["alice"]
    first, second = await create_run(client), await create_run(client, key="second")
    event = {"event_id": "same-local-event", "status": "running", "metrics": {"loss": 0.25}}
    path = f'/v1/research-runs/{first["id"]}/events'
    original = await client.post(path, json=event)
    assert original.status_code == 201
    assert (await client.post(path, json=event)).json() == original.json()
    conflict = await client.post(path, json={**event, "metrics": {"loss": 9}})
    assert conflict.status_code == 409
    independent = await client.post(f'/v1/research-runs/{second["id"]}/events', json=event)
    assert independent.status_code == 201
    assert independent.json()["id"] != original.json()["id"]
    detail = (await client.get(f'/v1/research-runs/{first["id"]}')).json()
    assert detail["events"] == [original.json()]
    assert detail["status"] == "running"
    assert detail["events"][0]["document"]["usage_source"] == "externally_reported"


async def test_event_validation_failure_is_atomic_and_key_can_be_retried(research_api):
    client = research_api["alice"]
    run = await create_run(client)
    path = f'/v1/research-runs/{run["id"]}/events'
    body = {"event_id": "recoverable", "status": "completed", "artifact_ids": ["missing-artifact"]}
    rejected = await client.post(path, json=body)
    assert rejected.status_code == 404
    detail = (await client.get(f'/v1/research-runs/{run["id"]}')).json()
    assert detail["status"] == "created"
    assert detail["events"] == []
    accepted = await client.post(path, json={**body, "artifact_ids": []})
    assert accepted.status_code == 201
    assert (await client.get(f'/v1/research-runs/{run["id"]}')).json()["events"] == [accepted.json()]


@pytest.mark.parametrize("reference", ["parent_run_id", "resume_checkpoint_id", "dataset_ids"])
async def test_foreign_lineage_references_are_rejected_without_partial_run(research_api, reference):
    original = await create_run(research_api["alice"])
    kind = {"parent_run_id": "run", "resume_checkpoint_id": "checkpoint", "dataset_ids": "dataset"}[reference]
    record_id = original["id"]
    if kind != "run":
        record_id = f"alice-private-{kind}"
        with research_api["repository"].db.session() as db:
            db.add(ResearchRecord(id=record_id, owner_id="alice", kind=kind,
                request_key=f"private-{kind}", content_hash="a" * 64, document={}))
    response = await research_api["bob"].post("/v1/research-runs",
        json={**RUN, reference: [record_id] if reference == "dataset_ids" else record_id},
        headers={"Idempotency-Key": "foreign-reference"})
    assert response.status_code == 404
    assert record_id not in response.text
    assert (await research_api["bob"].get("/v1/research-runs")).json()["items"] == []
    assert (await research_api["alice"].get("/v1/research-runs")).json()["items"] == [original]


@pytest.mark.parametrize("mutation", ["update", "delete"])
async def test_research_history_cannot_be_rewritten_and_rollback_preserves_future_writes(research_api, mutation):
    client = research_api["alice"]
    original = await create_run(client)
    with pytest.raises(RuntimeError, match="append-only"):
        with research_api["repository"].db.session() as db:
            row = db.get(ResearchRecord, original["id"])
            assert row is not None
            if mutation == "update":
                row.document = {**row.document, "code_revision": "rewritten"}
            else:
                db.delete(row)
    assert (await client.get("/v1/research-runs")).json()["items"] == [original]
    followup = await create_run(client, key="after-rollback")
    with research_api["repository"].db.session() as db:
        assert set(db.scalars(select(ResearchRecord.id).where(ResearchRecord.kind == "run"))) == {
            original["id"], followup["id"],
        }


@pytest.mark.parametrize("token,path,status", [
    ("research-only", "/v1/research-runs", 200),
    ("research-only", "/v1/benchmark-suites", 403),
    ("research-only", "/v1/evaluations", 403),
    ("evaluation-only", "/v1/benchmark-suites", 200),
    ("evaluation-only", "/v1/evaluations", 200),
    ("evaluation-only", "/v1/research-runs", 403),
    ("operator", "/v1/research-runs", 200),
    ("operator", "/v1/benchmark-suites", 200),
])
async def test_read_endpoints_require_their_specific_scope(research_api, token, path, status):
    response = await research_api[token].get(path)
    assert response.status_code == status
    if status == 200:
        assert response.json() == {"items": []}


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "limit=1&offset=-1", "limit=not-a-number"])
async def test_invalid_pagination_is_rejected_without_silently_broadening_access(research_api, query):
    await create_run(research_api["alice"])
    response = await research_api["alice"].get(f"/v1/research-runs?{query}")
    assert response.status_code == 422
