"""Transport regressions: rejected input must never execute application code."""

import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from adversarial_agent_mvp import research_auth
from adversarial_agent_mvp.research_auth import ResearchAuthMiddleware

pytestmark = pytest.mark.integration


def boundary(settings):
    delivered = []

    async def application(scope, receive, send):
        request = Request(scope, receive)
        delivered.append({"body": await request.body(), "identity": scope.get("state", {}).get("research_principal")})
        await JSONResponse({"accepted": True})(scope, receive, send)

    return ResearchAuthMiddleware(application, settings), delivered


async def chunks(*parts):
    for part in parts:
        yield part


@pytest.mark.parametrize("authorization", [None, "", "Basic alice", "Bearer", "Bearer ",
    "Bearer invalid", "Bearer alice ", "Bearer alice extra"])
async def test_invalid_credentials_never_reach_application(research_settings, authorization):
    app, delivered = boundary(research_settings)
    headers = {} if authorization is None else {"Authorization": authorization}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/research-runs", headers=headers, json={})
    assert response.status_code == 401
    assert response.json() == {"detail": "valid research bearer token required"}
    assert delivered == []


async def test_configured_credentials_disable_anonymous_development_fallback(research_settings):
    app, delivered = boundary(research_settings.model_copy(update={"research_auth_required": False}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.get("/v1/research-runs")
        accepted = await client.get("/v1/research-runs", headers={"Authorization": "Bearer alice"})
    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert [request["identity"]["owner_id"] for request in delivered] == ["alice"]


async def test_rate_limit_is_shared_by_rotated_tokens_isolated_by_owner_and_resets(research_settings, monkeypatch):
    clock = SimpleNamespace(now=6000.0)
    monkeypatch.setattr(research_auth, "time", SimpleNamespace(time=lambda: clock.now))
    app, delivered = boundary(research_settings.model_copy(update={"research_requests_per_minute": 2}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async def get(token):
            return await client.get("/v1/research-runs", headers={"Authorization": f"Bearer {token}"})

        assert (await get("alice")).status_code == 200
        assert (await get("alice-rotated")).status_code == 200
        clock.now = 6059.99
        limited = await get("alice")
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"
        assert limited.json() == {"detail": "request limit exceeded"}
        assert (await get("bob")).status_code == 200
        clock.now = 6060
        assert (await get("alice-rotated")).status_code == 200
    assert [request["identity"]["owner_id"] for request in delivered] == ["alice", "alice", "bob", "alice"]


@pytest.mark.parametrize("extra_bytes,status", [(0, 200), (1, 413)])
@pytest.mark.parametrize("method", ["POST", "PUT"])
async def test_chunked_json_enforces_exact_byte_limit_without_trusting_content_length(
    research_settings, extra_bytes, status, method,
):
    limit = 1024
    app, delivered = boundary(research_settings.model_copy(update={"research_max_request_bytes": limit}))
    # The byte boundary divides a UTF-8 code point. Validation must wait for the complete body.
    body = json.dumps({"value": "é" * 500}, ensure_ascii=False, separators=(",", ":")).encode()
    body += b" " * (limit + extra_bytes - len(body))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, "/v1/research-runs", content=chunks(body[:11], body[11:1024], body[1024:]),
            headers={"Authorization": "Bearer alice", "Content-Type": "application/json", "Content-Length": "1"})
    assert response.status_code == status
    assert [request["body"] for request in delivered] == ([body] if status == 200 else [])


@pytest.mark.parametrize("depth,status", [(128, 200), (129, 422)])
async def test_json_nesting_limit_has_an_inclusive_valid_boundary(research_settings, depth, status):
    app, delivered = boundary(research_settings)
    body = ("[" * depth + "0" + "]" * depth).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/research-runs", content=body, headers={"Authorization": "Bearer alice"})
    assert response.status_code == status
    assert [request["body"] for request in delivered] == ([body] if status == 200 else [])


@pytest.mark.parametrize("body", [b'{"value":"\xff"}', b'{"value":', b'{"value":1} trailing',
    b'{"nested":[{"value":1e309}]}', b'{"nested":[{"value":NaN}]}'])
async def test_malformed_or_nonfinite_json_is_rejected_before_execution(research_settings, body):
    app, delivered = boundary(research_settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/research-runs", content=chunks(body[:5], body[5:]),
            headers={"Authorization": "Bearer alice"})
    assert response.status_code == 422
    assert delivered == []


async def test_disconnect_mid_body_does_not_execute_or_send_a_response(research_settings):
    app, delivered = boundary(research_settings)
    messages = iter([{"type": "http.request", "body": b'{"value":', "more_body": True},
                     {"type": "http.disconnect"}])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    await app({"type": "http", "method": "POST", "path": "/v1/research-runs",
               "headers": [(b"authorization", b"Bearer alice")]}, receive, send)
    assert delivered == []
    assert sent == []


async def test_streaming_upload_bypasses_json_parser_and_ordinary_body_limit(research_settings):
    app, delivered = boundary(research_settings.model_copy(update={"research_max_request_bytes": 1024}))
    body = b"\xff\x00checkpoint" * 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/v1/artifact-uploads/upload-example", content=chunks(body[:10], body[10:]),
            headers={"Authorization": "Bearer alice"})
    assert response.status_code == 200
    assert [request["body"] for request in delivered] == [body]
