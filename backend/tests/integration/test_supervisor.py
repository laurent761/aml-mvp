import httpx
from fastapi.testclient import TestClient

from adversarial_agent_mvp.capsule import ReconciliationResult
from adversarial_agent_mvp.contracts import (
    CapsuleHandle,
    CapsuleSpec,
    RuntimeContainmentProof,
)
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.supervisor import create_supervisor_app


class FakeRuntime:
    def __init__(self) -> None:
        self.reset_ids: list[str] = []
        self.destroyed_ids: list[str] = []
        self.gateway_headers: dict[str, str] = {}
        self.reconciled_with: set[str] | None = None

    async def reconcile_orphans(self, active_capsule_ids: set[str]):
        self.reconciled_with = active_capsule_ids
        return ReconciliationResult(discovered=2, orphaned=2, removed=2, active_capsules=0)

    async def create(self, spec: CapsuleSpec) -> CapsuleHandle:
        proof = RuntimeContainmentProof(
            capsule_id="capsule-1",
            episode_id=spec.episode_id,
            verified=True,
            network_internal=True,
            ownership_labels_verified=True,
            container_network_counts={"blue": 1, "target": 1},
            actual_member_count=2,
            unexpected_attachment_count=0,
            resource_fingerprint="c" * 64,
        )
        return CapsuleHandle(
            capsule_id="capsule-1",
            episode_id=spec.episode_id,
            target_container_id="target-1",
            network_id="network-1",
            blue_alias="blue",
            runtime_containment_proof=proof,
        )

    async def healthcheck(self, handle: CapsuleHandle) -> bool:
        return handle.capsule_id == "capsule-1"

    async def reset(self, handle: CapsuleHandle) -> None:
        self.reset_ids.append(handle.capsule_id)

    async def destroy(self, handle: CapsuleHandle) -> None:
        self.destroyed_ids.append(handle.capsule_id)

    async def gateway_request(
        self,
        handle: CapsuleHandle,
        method: str,
        path: str,
        *,
        headers,
        params,
        json_body,
    ) -> httpx.Response:
        assert path.startswith("/") and not path.startswith("//")
        self.gateway_headers = headers
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "connection": "close"},
            json={
                "capsule_id": handle.capsule_id,
                "method": method,
                "path": path,
                "params": params,
                "body": json_body,
            },
        )


def test_supervisor_requires_auth_and_runs_lifecycle() -> None:
    runtime = FakeRuntime()
    settings = Settings(capsule_supervisor_token="s" * 32)
    headers = {"Authorization": f"Bearer {settings.capsule_supervisor_token}"}
    spec = CapsuleSpec(
        episode_id="episode-1",
        image="target@sha256:" + "a" * 64,
        entrypoint=["run"],
    )

    with TestClient(create_supervisor_app(settings, runtime)) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["orphan_reconciliation"]["removed"] == 2
        assert runtime.reconciled_with == set()
        assert client.post("/v1/capsules", json=spec.model_dump(mode="json")).status_code == 401

        created = client.post(
            "/v1/capsules",
            headers=headers,
            json=spec.model_dump(mode="json"),
        )
        assert created.status_code == 201
        assert created.json()["episode_id"] == "episode-1"
        assert created.json()["supervisor_capability"] is None
        assert created.json()["gateway_ingress_url"] is None
        assert created.json()["runtime_containment_proof"]["verified"] is True

        health = client.get("/v1/capsules/capsule-1/health", headers=headers)
        assert health.json() == {"capsule_id": "capsule-1", "healthy": True}

        proxied = client.post(
            "/v1/capsules/capsule-1/gateway/v1/target/invoke?turn=1",
            headers={
                **headers,
                "Content-Type": "application/json",
                "Connection": "keep-alive",
                "X-Blue-Supervisor": "must-not-cross",
                "X-Correlation-ID": "correlation-1",
            },
            json={"message": "hello"},
        )
        assert proxied.status_code == 200
        assert proxied.json()["path"] == "/v1/target/invoke"
        assert runtime.gateway_headers == {
            "accept": "*/*",
            "content-type": "application/json",
            "x-correlation-id": "correlation-1",
        }
        assert "connection" not in proxied.headers

        assert client.post("/v1/capsules/capsule-1/reset", headers=headers).status_code == 200
        assert runtime.reset_ids == ["capsule-1"]

        assert client.delete("/v1/capsules/capsule-1", headers=headers).status_code == 204
        assert runtime.destroyed_ids == ["capsule-1"]
        assert client.get("/v1/capsules/capsule-1/health", headers=headers).status_code == 404
