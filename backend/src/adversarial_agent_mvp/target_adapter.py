from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scenarios import ScenarioRuntime

from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .contracts import (
    AttackTask,
    CapsuleHandle,
    DestinationRoute,
    GatewayEpisodeBootstrap,
    GatewayVerifierState,
    PolicyDocument,
    PublicObservation,
    RedAction,
    TargetManifest,
)


class TargetAdapter(Protocol):
    remote_blue: bool

    async def prepare(self, task: AttackTask) -> None: ...
    async def healthcheck(self) -> bool: ...
    async def reset(self, seed: int) -> PublicObservation: ...
    async def invoke(
        self, action: RedAction, turn: int
    ) -> tuple[PublicObservation, list[dict[str, Any]]]: ...
    async def drain_private_trace(self) -> dict[str, Any]: ...
    def verifier_state(self) -> GatewayVerifierState | None: ...
    async def close(self) -> None: ...


_BUILTIN_ROUTES: dict[str, DestinationRoute] = {
    "payments": DestinationRoute(
        service="payments",
        protocols=["mcp", "http"],
        operations=["payment.create", "payment.read", "payment.approve", "payment.cancel"],
        http_operations={
            "POST /payments": "payment.create",
            "GET /payments": "payment.read",
            "POST /payments/approve": "payment.approve",
            "DELETE /payments": "payment.cancel",
        },
        allowed_headers=["content-type"],
    ),
    "mail": DestinationRoute(
        service="mail",
        protocols=["mcp", "http"],
        operations=["email.send", "email.read"],
        http_operations={"POST /email": "email.send", "GET /email": "email.read"},
        allowed_headers=["content-type"],
    ),
    "customers": DestinationRoute(
        service="customers",
        protocols=["mcp", "http"],
        operations=["customer.lookup", "customer.search", "customer.update"],
        http_operations={
            "GET /customers": "customer.lookup",
            "POST /customers/search": "customer.search",
            "PATCH /customers": "customer.update",
        },
        allowed_headers=["content-type"],
    ),
    "memory": DestinationRoute(
        service="memory",
        protocols=["mcp", "http"],
        operations=["memory.read", "memory.write", "memory.delete"],
        http_operations={
            "GET /memory": "memory.read",
            "PUT /memory": "memory.write",
            "DELETE /memory": "memory.delete",
        },
        allowed_headers=["content-type"],
    ),
    "files": DestinationRoute(
        service="files",
        protocols=["mcp", "http"],
        operations=["file.write", "file.read", "file.delete", "file.list"],
        http_operations={
            "PUT /files": "file.write",
            "GET /files": "file.read",
            "DELETE /files": "file.delete",
            "GET /files/list": "file.list",
        },
        allowed_headers=["content-type"],
    ),
}


def destination_routes_for_manifest(manifest: TargetManifest) -> dict[str, DestinationRoute]:
    routes = dict(manifest.destination_routes)
    for alias in set(manifest.environment_aliases.values()):
        if alias in routes:
            continue
        default = _BUILTIN_ROUTES.get(alias)
        if default is None:
            raise ValueError(f"destination alias {alias!r} has no trusted route declaration")
        enabled_protocols = [
            protocol for protocol in default.protocols if protocol in manifest.tool_transports
        ]
        if not enabled_protocols:
            raise ValueError(f"destination alias {alias!r} has no enabled transport")
        routes[alias] = default.model_copy(update={"protocols": enabled_protocols})
    return routes


class HttpTargetAdapter:
    """HTTP black-box adapter; capsule mode uses only localhost Blue supervisor ingress."""

    def __init__(
        self,
        manifest: TargetManifest,
        client: httpx.AsyncClient | None = None,
        *,
        capsule_handle: CapsuleHandle | None = None,
        policies: list[PolicyDocument] | None = None,
        allow_direct: bool = False,
        scenario_loader: Callable[[AttackTask], ScenarioRuntime] | None = None,
        inference_recorder: Callable[[list[dict[str, Any]]], list[str]] | None = None,
    ):
        if capsule_handle is None and not allow_direct:
            raise ValueError("direct manifest URLs require explicit allow_direct=True")
        if capsule_handle is not None and (
            not capsule_handle.gateway_ingress_url or not capsule_handle.supervisor_capability
        ):
            raise ValueError("capsule handle has no trusted Blue ingress")
        self.manifest = manifest
        self.scenario_loader = scenario_loader
        self.inference_recorder = inference_recorder
        self.handle = capsule_handle
        self.policies = list(policies or [])
        self.remote_blue = capsule_handle is not None
        self.client = client or httpx.AsyncClient(
            timeout=manifest.resource_limits.timeout_seconds,
            trust_env=False,
        )
        self._owns_client = client is None
        self._prepared = False
        self._last_verifier: GatewayVerifierState | None = None
        self._trace_cursor = 0
        self._inference_cursor = 0
        self._intervention_cursor = 0

    @property
    def _supervisor_headers(self) -> dict[str, str]:
        if not self.handle or not self.handle.supervisor_capability:
            return {}
        value = self.handle.supervisor_capability
        if self.handle.gateway_auth_header == "Authorization" and not value.startswith("Bearer "):
            value = f"Bearer {value}"
        return {self.handle.gateway_auth_header: value}

    def _ingress(self, path: str) -> str:
        if not self.handle or not self.handle.gateway_ingress_url:
            raise RuntimeError("trusted gateway ingress is unavailable")
        return self.handle.gateway_ingress_url.rstrip("/") + path

    @property
    def _episode_id(self) -> str:
        if self.handle is None:
            raise RuntimeError("capsule handle is unavailable")
        return self.handle.episode_id

    async def prepare(self, task: AttackTask) -> None:
        if self.manifest.intervention_protocol and not task.scenario_version_id:
            raise ValueError("explicit intervention delivery requires a pinned scenario")
        if self.manifest.inference_profile and (
            not self.handle or self.handle.inference_profile != self.manifest.inference_profile
        ):
            raise ValueError("target inference profile is not pinned by the supervisor")
        scenario = None
        if task.scenario_version_id:
            if not self.remote_blue or self.scenario_loader is None:
                raise ValueError("scenario targets require trusted Blue and a scenario loader")
            scenario = self.scenario_loader(task)
        if not self.remote_blue:
            self._prepared = True
            return
        if self._prepared:
            return
        routes = destination_routes_for_manifest(self.manifest)
        target_base, health_path, invoke_path, reset_path = _capsule_target_endpoints(self.manifest)
        bootstrap = GatewayEpisodeBootstrap(
            episode_id=self._episode_id,
            seed=task.random_seed,
            policies=self.policies,
            destination_routes=routes,
            attack_task=task,
            identities=self.manifest.identity_context,
            target_base_url=target_base,
            target_health_path=health_path,
            target_invoke_path=invoke_path,
            target_reset_path=reset_path,
            inference_profile=self.manifest.inference_profile,
            target_timeout_seconds=self.manifest.resource_limits.timeout_seconds,
            intervention_surfaces=scenario.intervention_surfaces
            if scenario and self.manifest.intervention_protocol
            else None,
            **(
                scenario.model_dump(mode="json", exclude={"intervention_surfaces"})
                if scenario
                else {}
            ),
        )
        response = await self.client.post(
            self._ingress("/v1/admin/episodes"),
            headers=self._supervisor_headers,
            json=bootstrap.model_dump(mode="json"),
        )
        response.raise_for_status()
        self._prepared = True

    async def healthcheck(self) -> bool:
        if self.remote_blue:
            if not self._prepared:
                return False
            try:
                response = await self.client.get(
                    self._ingress(f"/v1/admin/episodes/{self._episode_id}/ready"),
                    headers=self._supervisor_headers,
                )
                return response.is_success and bool(response.json().get("ready"))
            except (httpx.HTTPError, ValueError):
                return False
        try:
            response = await self.client.get(str(self.manifest.healthcheck_url))
            return response.is_success
        except httpx.HTTPError:
            return False

    async def reset(self, seed: int) -> PublicObservation:
        if self.remote_blue:
            if not self._prepared:
                raise RuntimeError("target adapter is not prepared")
            response = await self.client.post(
                self._ingress(f"/v1/admin/episodes/{self._episode_id}/reset"),
                headers=self._supervisor_headers,
            )
            response.raise_for_status()
            target = response.json().get("target") or {}
            self._trace_cursor = 0
            self._last_verifier = None
            return PublicObservation(
                target_response=target.get("response"),
                visible_errors=list(target.get("visible_errors", [])),
                visible_tool_results=list(target.get("visible_tool_results", [])),
                turn_number=0,
                terminated=bool(target.get("terminated", False)),
            )
        if self.manifest.reset_url:
            response = await self.client.post(str(self.manifest.reset_url), json={"seed": seed})
            response.raise_for_status()
        return PublicObservation(turn_number=0)

    async def invoke(
        self, action: RedAction, turn: int
    ) -> tuple[PublicObservation, list[dict[str, Any]]]:
        payload = {
            "channel": action.channel,
            "payload": action.payload,
            "action_id": action.action_id,
        }
        if self.remote_blue:
            response = await self.client.post(
                self._ingress(f"/v1/target/{self._episode_id}/invoke"),
                headers=self._supervisor_headers,
                json=payload,
            )
            response.raise_for_status()
            envelope = response.json()
            body = envelope["target"]
            self._last_verifier = GatewayVerifierState.model_validate(envelope["verifier"])
            effects: list[dict[str, Any]] = []
        else:
            response = await self.client.post(str(self.manifest.invoke_url), json=payload)
            response.raise_for_status()
            body = response.json()
            effects = list(body.get("effect_attempts", []))
        observation = PublicObservation(
            target_response=body.get("response"),
            visible_errors=list(body.get("visible_errors", [])),
            visible_tool_results=list(body.get("visible_tool_results", [])),
            turn_number=turn,
            terminated=bool(body.get("terminated", False)),
            delivery_receipt=envelope.get("delivery_receipt") if self.remote_blue else None,
        )
        return observation, effects

    async def drain_private_trace(self) -> dict[str, Any]:
        if not self.remote_blue:
            return {"events": [], "verifier": None}
        response = await self.client.get(
            self._ingress(f"/v1/admin/episodes/{self._episode_id}/trace"),
            headers=self._supervisor_headers,
            params={
                "after": self._trace_cursor,
                "inference_after": self._inference_cursor,
                "intervention_after": self._intervention_cursor,
            },
        )
        response.raise_for_status()
        trace = response.json()
        intervention_events = list(trace.get("intervention_events", []))
        if intervention_events:
            self._intervention_cursor = max(int(event["sequence"]) for event in intervention_events)
        inference_events = list(trace.get("inference_events", []))
        if inference_events:
            if self.inference_recorder:
                trace["target_model_invocation_ids"] = self.inference_recorder(inference_events)
            self._inference_cursor = max(int(event["sequence"]) for event in inference_events)
        events = list(trace.get("events", []))
        if events:
            self._trace_cursor = max(int(event["sequence"]) for event in events)
        if trace.get("verifier"):
            self._last_verifier = GatewayVerifierState.model_validate(trace["verifier"])
        return trace

    def verifier_state(self) -> GatewayVerifierState | None:
        return self._last_verifier

    async def close(self) -> None:
        try:
            if self._prepared and self.remote_blue and self.manifest.inference_profile:
                response = await self.client.post(
                    self._ingress(f"/v1/admin/episodes/{self._episode_id}/drain"),
                    headers=self._supervisor_headers,
                )
                response.raise_for_status()
                deadline = (
                    asyncio.get_running_loop().time()
                    + 4 * self.manifest.inference_profile.timeout_seconds
                    + 20
                )
                while True:
                    trace = await self.drain_private_trace()
                    if not trace.get("inference_pending"):
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError("target inference did not settle before teardown")
                    await asyncio.sleep(0.2)
        finally:
            if self._owns_client:
                await self.client.aclose()


def _capsule_target_endpoints(
    manifest: TargetManifest,
) -> tuple[str, str, str, str | None]:
    endpoints = [manifest.healthcheck_url, manifest.invoke_url]
    if manifest.reset_url:
        endpoints.append(manifest.reset_url)
    parsed = [urlsplit(str(endpoint)) for endpoint in endpoints]
    first = parsed[0]
    port = first.port or (443 if first.scheme == "https" else 80)
    if first.scheme not in {"http", "https"}:
        raise ValueError("target endpoints must use HTTP")
    for endpoint in parsed[1:]:
        endpoint_port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
        if endpoint.scheme != first.scheme or endpoint_port != port:
            raise ValueError("target endpoints must share one internal scheme and port")

    def path(endpoint: Any) -> str:
        value = endpoint.path or "/"
        if endpoint.query:
            value += f"?{endpoint.query}"
        return value

    base = f"{first.scheme}://target:{port}"
    return (
        base,
        path(parsed[0]),
        path(parsed[1]),
        path(parsed[2]) if len(parsed) == 3 else None,
    )
