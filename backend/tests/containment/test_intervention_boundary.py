import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from adversarial_agent_mvp.blue_gateway import create_blue_app
from adversarial_agent_mvp.bundle_cli import reference_bundle
from adversarial_agent_mvp.contracts import AttackChannel, AttackTask, GatewayEpisodeBootstrap
from adversarial_agent_mvp.security import CapabilityTokenService
from tests.helpers import IsolatedSettings

pytestmark = pytest.mark.containment


@asynccontextmanager
async def gateway(handler):
    bundle = reference_bundle("sha256:" + "a" * 64)
    settings = IsolatedSettings(capability_signing_key="x" * 32, otel_enabled=False)
    app = create_blue_app(
        settings,
        supervisor_token="trusted-supervisor",
        target_transport=httpx.MockTransport(handler),
    )
    async with httpx.AsyncClient(
        base_url="http://blue:8080", transport=httpx.ASGITransport(app=app)
    ) as client:
        bootstrap = GatewayEpisodeBootstrap(
            episode_id="e1",
            seed=1,
            policies=[],
            destination_routes=bundle.manifest.destination_routes,
            attack_task=AttackTask(
                target_version_id="tv1",
                objective="test delivery",
                forbidden_states=bundle.ground_truth.forbidden_states,
                available_channels=[AttackChannel(s.channel) for s in bundle.scenario.surfaces],
            ),
            target_base_url="http://target:8081",
            target_health_path="/healthz",
            target_invoke_path="/invoke",
            target_reset_path="/reset",
            intervention_surfaces=list(bundle.scenario.surfaces),
            initial_world_state=bundle.ground_truth.initial_world_state,
            identities=bundle.manifest.identity_context,
        )
        # Enable both HTTP facades for the same declared file.read slot.
        bootstrap.destination_routes["documents"].protocols = ["mcp", "http"]
        bootstrap.destination_routes["documents"].http_operations = {"POST /read": "file.read"}
        headers = {"X-Blue-Supervisor": "trusted-supervisor"}
        response = await client.post(
            "/v1/admin/episodes", headers=headers, json=bootstrap.model_dump(mode="json")
        )
        assert response.status_code == 201
        token = CapabilityTokenService(settings.capability_signing_key).issue("e1", ["documents"])
        yield client, headers, {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("reject", "target_rejected"),
        ("timeout", "delivery_unknown"),
        ("bad_ack", "delivery_unknown"),
        ("fake_receipt", "delivery_unknown"),
        ("valid_ack", "applied"),
    ],
)
async def test_only_matching_target_acknowledgement_confirms_message_delivery(kind, expected):
    def target(request):
        if kind == "reject":
            return httpx.Response(422, json={"detail": "target refuses this message"})
        if kind == "timeout":
            raise httpx.ReadTimeout("private transport diagnostic")
        plan = json.loads(request.content)["delivery"]
        plan.pop("protocol")
        if kind == "bad_ack":
            plan["slot_id"] = "another-surface"
        if kind == "fake_receipt":
            return httpx.Response(
                200,
                json={"response": "ok", "delivery_receipt": {"applied": True, "status": "applied"}},
            )
        return httpx.Response(200, json={"response": "ok", "intervention_ack": plan})

    async with gateway(target) as (client, headers, capability):
        payload = {"action_id": "a1", "channel": "user_message", "payload": {"text": "hello"}}
        response = await client.post("/v1/target/e1/invoke", headers=headers, json=payload)
        assert response.status_code == 200
        receipt = response.json()["delivery_receipt"]
        assert receipt["status"] == expected
        assert receipt["applied"] == (expected == "applied")
        assert "private transport diagnostic" not in response.text
        assert "intervention_ack" not in response.json()["target"]
        assert "delivery_receipt" not in response.json()["target"]
        assert (
            await client.get("/v1/admin/episodes/e1/trace", headers=capability)
        ).status_code == 403
        trace = await client.get("/v1/admin/episodes/e1/trace", headers=headers)
        assert trace.json()["intervention_events"][0]["receipt_id"] == receipt["receipt_id"]
        assert not trace.json()["verifier"]["terminal_success"]


@pytest.mark.parametrize(
    "body",
    [
        b"{invalid",
        b"[]",
        b'{"action_id":2,"channel":"user_message","payload":{}}',
        b'{"action_id":"\\ud800","channel":"user_message","payload":{"text":"hello"}}',
        b"x" * 22000,
    ],
)
async def test_malformed_envelopes_receive_receipts_without_invoking_target(body):
    async with gateway(lambda r: pytest.fail("invalid input reached target")) as (
        client,
        headers,
        _,
    ):
        response = await client.post(
            "/v1/target/e1/invoke",
            headers={**headers, "content-type": "application/json"},
            content=body,
        )
        assert response.status_code == 200
        assert response.json()["delivery_receipt"]["status"] == "malformed_action"


@pytest.mark.parametrize("transport", ["mcp", "rpc", "http", "http_alias"])
async def test_all_tool_transports_apply_only_the_current_declared_slot_after_verification(
    transport,
):
    arrived, release = asyncio.Event(), asyncio.Event()
    plans = []

    async def target(request):
        document = json.loads(request.content)
        plans.append(document["delivery"])
        assert document["payload"] == {"slot_id": "invoice-content"}
        assert "REPLACEMENT-CANARY" not in request.content.decode()
        arrived.set()
        await release.wait()
        return httpx.Response(200, json={"response": "done"})

    async with gateway(target) as (client, headers, capability):
        action = {
            "action_id": "a1",
            "channel": "simulated_tool_result",
            "payload": {"slot_id": "invoice-content", "content": "REPLACEMENT-CANARY"},
        }
        invocation = asyncio.create_task(
            client.post("/v1/target/e1/invoke", headers=headers, json=action)
        )
        try:
            await arrived.wait()

            async def call(correlation, delivery_id):
                auth = {
                    **capability,
                    "X-Intervention-ID": delivery_id,
                    "X-Correlation-ID": correlation,
                }
                if transport == "mcp":
                    return await client.post(
                        "/v1/mcp/tools/call",
                        headers=auth,
                        json={
                            "episode_id": "e1",
                            "destination_alias": "documents",
                            "tool_name": "file.read",
                            "arguments": {"file_id": "invoice-001"},
                            "correlation_id": correlation,
                        },
                    )
                if transport == "rpc":
                    return await client.post(
                        "/mcp/e1/documents",
                        headers=auth,
                        json={
                            "jsonrpc": "2.0",
                            "id": correlation,
                            "method": "tools/call",
                            "params": {
                                "name": "file.read",
                                "arguments": {"file_id": "invoice-001"},
                            },
                        },
                    )
                if transport == "http":
                    return await client.post(
                        "/v1/http/effects",
                        headers=auth,
                        json={
                            "episode_id": "e1",
                            "destination_alias": "documents",
                            "method": "POST",
                            "path": "/read",
                            "body": {"file_id": "invoice-001"},
                            "correlation_id": correlation,
                        },
                    )
                return await client.post(
                    "/http/e1/documents/read", headers=auth, json={"file_id": "invoice-001"}
                )

            wrong = await call("wrong-delivery", "an-old-or-different-action")
            assert wrong.status_code == 200 and "REPLACEMENT-CANARY" not in wrong.text
            delivery_id = plans[0]["delivery_id"]
            first = await call("same-call", delivery_id)
            repeated = await call("same-call", delivery_id)
            assert first.status_code == 200 and first.json() == repeated.json()
            assert "REPLACEMENT-CANARY" in first.text
            second = await call("next-call", delivery_id)
            assert second.status_code == 200 and "REPLACEMENT-CANARY" not in second.text
            release.set()
            completed = (await invocation).json()
            assert completed["delivery_receipt"]["status"] == "applied"
            assert completed["delivery_receipt"]["evidence"] == "gateway_tool_response"
            assert "REPLACEMENT-CANARY" not in (await call("later-call", delivery_id)).text
            # A cached modified response cannot be replayed in a different action.
            assert (await call("same-call", "another-action")).status_code == 403
            state = (await client.get("/v1/admin/episodes/e1/state", headers=headers)).json()
            assert "REPLACEMENT-CANARY" not in json.dumps(state)
            trace = (await client.get("/v1/admin/episodes/e1/trace", headers=headers)).json()
            assert len(trace["intervention_events"]) == 1
            assert "REPLACEMENT-CANARY" not in json.dumps(trace["events"])
            assert not trace["verifier"]["terminal_success"]
        finally:
            release.set()
            await invocation


@pytest.mark.parametrize("failure", ["timeout", "server_error", "out_of_band_effects"])
async def test_applied_receipt_does_not_hide_subsequent_target_failure(failure):
    async def target(request):
        plan = json.loads(request.content)["delivery"]
        tool = await client.post(
            "/v1/mcp/tools/call",
            headers={**capability, "X-Intervention-ID": plan["delivery_id"]},
            json={
                "episode_id": "e1",
                "destination_alias": "documents",
                "tool_name": "file.read",
                "arguments": {"file_id": "invoice-001"},
                "correlation_id": "read-before-failure",
            },
        )
        assert tool.status_code == 200 and tool.json()["result"]["file"]["content"] == "delivered"
        if failure == "timeout":
            raise httpx.ReadTimeout("private diagnostics")
        if failure == "server_error":
            return httpx.Response(500, json={"detail": "private diagnostics"})
        return httpx.Response(
            200, json={"effect_attempts": [{"private_events": [{"terminal_success": True}]}]}
        )

    async with gateway(target) as (client, headers, capability):
        response = await client.post(
            "/v1/target/e1/invoke",
            headers=headers,
            json={
                "action_id": "a1",
                "channel": "simulated_tool_result",
                "payload": {"slot_id": "invoice-content", "content": "delivered"},
            },
        )
        body = response.json()
        assert body["delivery_receipt"]["applied"]
        assert body["delivery_receipt"]["delivered_content"] == "delivered"
        assert body["target"]["visible_errors"] and body["target"]["terminated"]
        assert not body["verifier"]["terminal_success"]
        assert "private diagnostics" not in response.text
