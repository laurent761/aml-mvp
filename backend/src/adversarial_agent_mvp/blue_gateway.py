from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .blue import BlueEngine, normalize_http_effect, normalize_mcp_effect
from .contracts import (
    DestinationRoute,
    EffectAttempt,
    GatewayEpisodeBootstrap,
    GatewayVerifierState,
    PublicEffectResponse,
)
from .inference_contracts import INFERENCE_DESTINATION, InferenceResult, TargetInferenceRequest
from .inference_mailbox import InferenceMailbox
from .interventions import InterventionDelivery
from .policy import PolicyEngine
from .security import CapabilityError, CapabilityTokenService
from .settings import Settings, get_settings
from .telemetry import configure_telemetry, instrument_fastapi
from .verifier import DeterministicVerifier, calculate_reward
from .virtual_world import VirtualWorld


class HttpEffectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    episode_id: str
    destination_alias: str
    method: str
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str


class McpToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    episode_id: str
    destination_alias: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str


@dataclass(slots=True)
class GatewayEpisode:
    bootstrap: GatewayEpisodeBootstrap
    engine: BlueEngine
    verifier: DeterministicVerifier
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    target_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    draining: bool = False
    sequence: int = 0
    idempotency: dict[str, tuple[str, PublicEffectResponse]] = field(default_factory=dict)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    inference: InferenceMailbox | None = None
    interventions: InterventionDelivery | None = None


class GatewayRegistry:
    """Trusted, episode-scoped Blue state. Unknown episodes always fail closed."""

    def __init__(self) -> None:
        self._episodes: dict[str, GatewayEpisode] = {}
        self._registry_lock = asyncio.Lock()

    async def bootstrap(self, config: GatewayEpisodeBootstrap) -> None:
        _validate_target_endpoint(config)
        if INFERENCE_DESTINATION in config.destination_routes:
            raise ValueError("inference is not a virtual business destination")
        verifier = DeterministicVerifier()
        await verifier.initialize(config.attack_task)
        world = VirtualWorld(
            destination_routes=config.destination_routes,
            initial_state=config.initial_world_state,
            trusted_context={
                "identities": config.identities,
                "input_provenance": "untrusted_input",
            },
        )
        await world.reset(config.seed)
        episode = GatewayEpisode(
            bootstrap=config,
            engine=BlueEngine(PolicyEngine(config.policies, capsule_mode=True), world),
            verifier=verifier,
            inference=InferenceMailbox(config.episode_id, config.inference_profile)
            if config.inference_profile
            else None,
            interventions=InterventionDelivery(
                config.episode_id, config.scenario_version_id, config.intervention_surfaces
            )
            if config.intervention_surfaces is not None
            else None,
        )
        async with self._registry_lock:
            if config.episode_id in self._episodes:
                raise ValueError("episode is already registered")
            self._episodes[config.episode_id] = episode

    def get(self, episode_id: str) -> GatewayEpisode:
        episode = self._episodes.get(episode_id)
        if episode is None:
            raise KeyError("episode is not registered")
        return episode

    async def reset(self, episode_id: str) -> None:
        episode = self.get(episode_id)
        async with episode.lock:
            if episode.interventions and episode.interventions.active:
                episode.interventions.finish(
                    episode.interventions.active,
                    failure="delivery_unknown",
                    reason="reset_interrupted_delivery",
                )
            await episode.engine.world.reset(episode.bootstrap.seed)
            await episode.verifier.initialize(episode.bootstrap.attack_task)
            episode.idempotency.clear()
            episode.audit_events.clear()
            episode.sequence = 0
            episode.draining = False

    async def drain(self, episode_id: str) -> list[dict[str, Any]]:
        episode = self.get(episode_id)
        episode.draining = True
        async with episode.lock:
            return list(episode.audit_events)

    async def remove(self, episode_id: str) -> None:
        episode = self.get(episode_id)
        episode.draining = True
        if episode.inference:
            episode.inference.close()
        async with episode.lock:
            async with self._registry_lock:
                self._episodes.pop(episode_id, None)

    async def process(
        self, effect: EffectAttempt, intervention_id: str | None = None
    ) -> PublicEffectResponse:
        episode = self.get(effect.episode_id)
        route = _route_for(episode, effect.destination_alias, effect.protocol, effect.operation)
        trusted_effect = effect.model_copy(update={"identity_alias": route.identity_alias})
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "effect": trusted_effect.model_dump(mode="json", exclude={"effect_id"}),
                    "intervention_id": intervention_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with episode.lock:
            if episode.draining:
                raise RuntimeError("episode is draining")
            previous = episode.idempotency.get(effect.correlation_id)
            if previous:
                if previous[0] != fingerprint:
                    raise ValueError("correlation_id was reused with different input")
                return previous[1]
            outcome = await episode.engine.process(trusted_effect)
            for private_event in outcome.result.private_events:
                await episode.verifier.process_event(private_event)
            episode.sequence += 1
            signals = episode.verifier.signals()
            public = PublicEffectResponse(
                effect_id=trusted_effect.effect_id,
                success=outcome.result.success,
                result=outcome.result.result,
            )
            episode.audit_events.append(
                {
                    "sequence": episode.sequence,
                    "effect": trusted_effect.model_dump(mode="json"),
                    "decision": outcome.decision.model_dump(mode="json"),
                    "virtual_result": outcome.result.model_dump(mode="json"),
                    "verifier_signals": [signal.model_dump(mode="json") for signal in signals],
                }
            )
            if episode.interventions:
                public = episode.interventions.overlay(trusted_effect, public, intervention_id)
            episode.idempotency[effect.correlation_id] = (fingerprint, public)
            return public

    def verifier_state(self, episode_id: str) -> GatewayVerifierState:
        episode = self.get(episode_id)
        signals = episode.verifier.signals()
        terminal = episode.verifier.terminal_signal()
        return GatewayVerifierState(
            reward=calculate_reward(signals, step=max(episode.sequence, 1)),
            terminal_success=terminal is not None,
            evidence_ref=terminal.evidence_refs[-1]
            if terminal and terminal.evidence_refs
            else None,
            signals=signals,
        )


def _validate_target_endpoint(config: GatewayEpisodeBootstrap) -> None:
    endpoint = urlsplit(config.target_base_url)
    if endpoint.scheme not in {"http", "https"} or endpoint.hostname != "target":
        raise ValueError("target_base_url must address the capsule target alias")
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("target_base_url contains unsupported components")
    for path in (
        config.target_health_path,
        config.target_invoke_path,
        config.target_reset_path,
    ):
        if path is not None and (not path.startswith("/") or path.startswith("//")):
            raise ValueError("target paths must be absolute local paths")


def _route_for(
    episode: GatewayEpisode,
    alias: str,
    protocol: str,
    operation: str,
) -> DestinationRoute:
    route = episode.bootstrap.destination_routes.get(alias)
    if route is None:
        raise ValueError("unknown destination alias")
    if protocol not in route.protocols or operation not in route.operations:
        raise ValueError("operation is outside the destination capability")
    return route


def _operation_for_http(route: DestinationRoute, method: str, path: str) -> str:
    normalized_path = "/" + path.lstrip("/")
    operation = route.http_operations.get(f"{method.upper()} {normalized_path}")
    if operation is None:
        raise ValueError("HTTP method/path is outside the destination capability")
    return operation


def _target_url(episode: GatewayEpisode, path: str) -> str:
    return episode.bootstrap.target_base_url.rstrip("/") + path


def create_blue_app(
    settings: Settings | None = None,
    *,
    supervisor_token: str | None = None,
    target_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    tokens = CapabilityTokenService(settings.capability_signing_key)
    registry = GatewayRegistry()
    configured_supervisor_token = (
        supervisor_token or os.getenv("BLUE_SUPERVISOR_TOKEN") or secrets.token_urlsafe(48)
    )
    telemetry = configure_telemetry(settings, service_name="blue-gateway")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            telemetry.shutdown()

    app = FastAPI(title="Trusted Blue Gateway", version="0.2.0", lifespan=lifespan)
    app.state.registry = registry

    def authorize_target(authorization: str | None, episode_id: str, destination: str) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="capability token required")
        try:
            tokens.verify(
                authorization.removeprefix("Bearer "),
                episode_id=episode_id,
                destination=destination,
            )
        except CapabilityError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def authorize_supervisor(value: str | None) -> None:
        if not value or not hmac.compare_digest(value, configured_supervisor_token):
            raise HTTPException(status_code=403, detail="supervisor capability required")

    def episode_or_404(episode_id: str) -> GatewayEpisode:
        try:
            return registry.get(episode_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def execute(
        effect: EffectAttempt, intervention_id: str | None = None
    ) -> PublicEffectResponse:
        try:
            return await registry.process(effect, intervention_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def call_target(
        episode: GatewayEpisode,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=target_transport,
            timeout=episode.bootstrap.target_timeout_seconds,
            trust_env=False,
        ) as client:
            return await client.request(method, _target_url(episode, path), json=body)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/admin/episodes", status_code=201)
    async def bootstrap_episode(
        payload: GatewayEpisodeBootstrap,
        x_blue_supervisor: str | None = Header(default=None),
    ) -> dict[str, str]:
        authorize_supervisor(x_blue_supervisor)
        try:
            await registry.bootstrap(payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"episode_id": payload.episode_id, "status": "ready"}

    @app.get("/v1/admin/episodes/{episode_id}/ready")
    async def episode_ready(
        episode_id: str,
        x_blue_supervisor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize_supervisor(x_blue_supervisor)
        episode = episode_or_404(episode_id)
        response = await call_target(episode, "GET", episode.bootstrap.target_health_path)
        return {"ready": response.is_success, "target_status": response.status_code}

    @app.post("/v1/admin/episodes/{episode_id}/reset")
    async def reset_episode(
        episode_id: str,
        x_blue_supervisor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize_supervisor(x_blue_supervisor)
        episode = episode_or_404(episode_id)
        # This lock is separate from effect processing: a target invocation calls
        # Blue tools while in flight. Holding the effect lock would deadlock it.
        async with episode.target_lock:
            episode.draining = True
            target_result: dict[str, Any] = {}
            if episode.bootstrap.target_reset_path:
                response = await call_target(
                    episode,
                    "POST",
                    episode.bootstrap.target_reset_path,
                    {
                        "seed": episode.bootstrap.seed,
                        **(
                            {"configuration": episode.bootstrap.target_configuration}
                            if episode.bootstrap.target_configuration
                            else {}
                        ),
                        **(
                            {
                                "intervention_surfaces": [
                                    s.model_dump(mode="json")
                                    for s in episode.bootstrap.intervention_surfaces
                                ]
                            }
                            if episode.bootstrap.intervention_surfaces is not None
                            else {}
                        ),
                    },
                )
                if not response.is_success:
                    raise HTTPException(status_code=502, detail="target reset failed")
                target_result = response.json() if response.content else {}
            await registry.reset(episode_id)
            return {"episode_id": episode_id, "target": target_result, "status": "ready"}

    @app.get("/v1/admin/episodes/{episode_id}/trace")
    async def private_trace(
        episode_id: str,
        after: int = Query(default=0, ge=0),
        inference_after: int = Query(default=0, ge=0),
        intervention_after: int = Query(default=0, ge=0),
        x_blue_supervisor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize_supervisor(x_blue_supervisor)
        episode = episode_or_404(episode_id)
        return {
            "intervention_events": [
                r for r in episode.interventions.records if r["sequence"] > intervention_after
            ]
            if episode.interventions
            else [],
            "events": [event for event in episode.audit_events if event["sequence"] > after],
            "verifier": registry.verifier_state(episode_id).model_dump(mode="json"),
            "inference_events": [
                record
                for record in episode.inference.records
                if record["sequence"] > inference_after
            ]
            if episode.inference
            else [],
            "inference_pending": sum(
                not pending.future.done() for pending in episode.inference.requests.values()
            )
            if episode.inference
            else 0,
        }

    @app.post("/v1/inference/chat/completions")
    async def target_inference(
        request: Request,
        authorization: str | None = Header(default=None),
        x_episode_id: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not x_episode_id or not x_correlation_id:
            raise HTTPException(400, "episode and correlation IDs are required")
        authorize_target(authorization, x_episode_id, INFERENCE_DESTINATION)
        episode = episode_or_404(x_episode_id)
        if episode.inference is None or episode.inference.closed or episode.draining:
            raise HTTPException(409, "target inference is unavailable")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > episode.inference.profile.max_input_bytes + 16384:
                raise HTTPException(413, "inference input limit exceeded")
            chunks.append(chunk)
        try:
            payload = TargetInferenceRequest.model_validate_json(b"".join(chunks))
            result = await episode.inference.submit(x_correlation_id, payload)
        except ValueError as exc:
            raise HTTPException(422, "invalid inference request or correlation conflict") from exc
        except OverflowError as exc:
            raise HTTPException(429, str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(504, "inference request timed out") from exc
        if result.audit.status != "SUCCEEDED":
            code = (
                429
                if result.audit.status == "REJECTED"
                else 504
                if result.audit.status == "TIMED_OUT"
                else 502
            )
            raise HTTPException(code, result.audit.error_code or "target inference failed")
        return {
            "id": result.request_id,
            "model": episode.inference.profile.model,
            "choices": [{"message": {"role": "assistant", "content": result.content}}],
        }

    @app.post("/v1/admin/episodes/{episode_id}/inference/claim")
    async def claim_inference(
        episode_id: str, x_blue_supervisor: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorize_supervisor(x_blue_supervisor)
        episode = episode_or_404(episode_id)
        return {
            "requests": [work.model_dump(mode="json") for work in episode.inference.claim()]
            if episode.inference
            else []
        }

    @app.post("/v1/admin/episodes/{episode_id}/inference/complete")
    async def complete_inference(
        episode_id: str,
        result: InferenceResult,
        x_blue_supervisor: str | None = Header(default=None),
    ) -> dict[str, str]:
        authorize_supervisor(x_blue_supervisor)
        episode = episode_or_404(episode_id)
        if episode.inference is None:
            raise HTTPException(409, "target inference is unavailable")
        try:
            episode.inference.complete(result)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "recorded"}

    @app.post("/v1/admin/episodes/{episode_id}/inference/fail")
    async def fail_inference(
        episode_id: str, x_blue_supervisor: str | None = Header(default=None)
    ) -> dict[str, str]:
        authorize_supervisor(x_blue_supervisor)
        episode = episode_or_404(episode_id)
        if episode.inference:
            episode.inference.fail_pending()
        return {"status": "closed"}

    @app.get("/v1/admin/episodes/{episode_id}/state")
    async def private_state(
        episode_id: str,
        x_blue_supervisor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize_supervisor(x_blue_supervisor)
        episode = episode_or_404(episode_id)
        async with episode.lock:
            return await episode.engine.world.snapshot_state()

    @app.post("/v1/admin/episodes/{episode_id}/drain")
    async def drain_episode(
        episode_id: str,
        x_blue_supervisor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize_supervisor(x_blue_supervisor)
        try:
            events = await registry.drain(episode_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"episode_id": episode_id, "status": "drained", "events": events}

    @app.delete("/v1/admin/episodes/{episode_id}", status_code=204)
    async def teardown_episode(
        episode_id: str,
        x_blue_supervisor: str | None = Header(default=None),
    ) -> None:
        authorize_supervisor(x_blue_supervisor)
        try:
            await registry.remove(episode_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/target/{episode_id}/invoke")
    async def invoke_target(
        episode_id: str,
        request: Request,
        x_blue_supervisor: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize_supervisor(x_blue_supervisor)
        episode = episode_or_404(episode_id)
        async with episode.target_lock:
            if episode.draining:
                raise HTTPException(status_code=409, detail="episode is draining")
            if episode.interventions:
                delivery = episode.interventions
                chunks, size, malformed = [], 0, None
                limit = (
                    max((s.max_payload_bytes for s in delivery.surfaces.values()), default=8192)
                    + 4096
                )
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        malformed = "request_size_limit"
                        break
                    chunks.append(chunk)
                try:
                    document = json.loads(b"".join(chunks)) if not malformed else None
                except (ValueError, RecursionError):
                    document, malformed = None, "invalid_json"
                try:
                    attempt = delivery.prepare(document, malformed_reason=malformed)
                except RuntimeError as exc:
                    raise HTTPException(409, str(exc)) from exc
                body: dict[str, Any] = {}
                if attempt.receipt is None:
                    try:
                        response = await call_target(
                            episode,
                            "POST",
                            episode.bootstrap.target_invoke_path,
                            attempt.target_body(),
                        )
                        if response.is_success:
                            body = response.json()
                            if not isinstance(body, dict):
                                raise ValueError("invalid target response")
                            if body.get("effect_attempts"):
                                body = {
                                    "visible_errors": [
                                        "target returned forbidden out-of-band effects"
                                    ],
                                    "terminated": True,
                                }
                                delivery.finish(
                                    attempt,
                                    failure="target_rejected",
                                    reason="out_of_band_effect_reports",
                                )
                            else:
                                delivery.finish(attempt, body)
                        else:
                            body = {
                                "visible_errors": ["target rejected or failed the invocation"],
                                "terminated": response.status_code >= 500,
                            }
                            delivery.finish(
                                attempt,
                                failure="target_rejected"
                                if 400 <= response.status_code < 500
                                else "delivery_unknown",
                                reason="target_rejected_action"
                                if 400 <= response.status_code < 500
                                else "target_execution_failed",
                            )
                    except (httpx.HTTPError, ValueError, TypeError):
                        body = {
                            "visible_errors": ["target transport or response failed"],
                            "terminated": True,
                        }
                        delivery.finish(
                            attempt,
                            failure="delivery_unknown",
                            reason="target_transport_or_response_error",
                        )
                    except BaseException:
                        delivery.finish(
                            attempt,
                            failure="delivery_unknown",
                            reason="target_invocation_interrupted",
                        )
                        raise
                receipt = delivery.finish(attempt)
                body.pop("intervention_ack", None)
                body.pop("delivery_receipt", None)
                body.pop("effect_attempts", None)
                if not receipt.applied:
                    body["visible_errors"] = [f"intervention {receipt.status}: {receipt.reason}"]
                if receipt.status == "delivery_unknown":
                    body["terminated"] = True
                return {
                    "target": body,
                    "delivery_receipt": receipt.model_dump(mode="json"),
                    "verifier": registry.verifier_state(episode_id).model_dump(mode="json"),
                }
            response = await call_target(
                episode,
                "POST",
                episode.bootstrap.target_invoke_path,
                await request.json(),
            )
            if not response.is_success:
                raise HTTPException(status_code=502, detail="target invocation failed")
            body = response.json()
            if body.get("effect_attempts"):
                raise HTTPException(
                    status_code=409,
                    detail="out-of-band effect reports are forbidden; target tools must call Blue",
                )
            body.pop("effect_attempts", None)
            return {
                "target": body,
                "verifier": registry.verifier_state(episode_id).model_dump(mode="json"),
            }

    @app.api_route(
        "/http/{episode_id}/{destination_alias}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        response_model=PublicEffectResponse,
    )
    @app.api_route(
        "/http/{episode_id}/{destination_alias}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        response_model=PublicEffectResponse,
    )
    async def aliased_http_effect(
        episode_id: str,
        destination_alias: str,
        request: Request,
        path: str = "",
        authorization: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None),
        x_intervention_id: str | None = Header(default=None),
    ) -> PublicEffectResponse:
        authorize_target(authorization, episode_id, destination_alias)
        if not x_correlation_id:
            raise HTTPException(status_code=400, detail="X-Correlation-ID is required")
        episode = episode_or_404(episode_id)
        route = episode.bootstrap.destination_routes.get(destination_alias)
        if route is None or "http" not in route.protocols:
            raise HTTPException(status_code=403, detail="HTTP destination is not allowed")
        normalized_path = "/" + path.lstrip("/")
        try:
            operation = _operation_for_http(route, request.method, normalized_path)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        body: dict[str, Any] = {}
        if await request.body():
            try:
                parsed = await request.json()
                if not isinstance(parsed, dict):
                    raise ValueError
                body = parsed
            except (ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(
                    status_code=422, detail="HTTP tool body must be JSON object"
                ) from exc
        allowed_headers = {name.lower() for name in route.allowed_headers}
        body["headers"] = {
            key: value for key, value in request.headers.items() if key.lower() in allowed_headers
        }
        effect = normalize_http_effect(
            episode_id=episode_id,
            destination_alias=destination_alias,
            method=request.method,
            path=normalized_path,
            body={"operation": operation, "arguments": body},
            identity_alias=None,
            correlation_id=x_correlation_id,
        )
        return await execute(effect, x_intervention_id)

    @app.post("/v1/http/effects", response_model=PublicEffectResponse)
    async def http_effect(
        payload: HttpEffectRequest,
        authorization: str | None = Header(default=None),
        x_intervention_id: str | None = Header(default=None),
    ) -> PublicEffectResponse:
        authorize_target(authorization, payload.episode_id, payload.destination_alias)
        episode = episode_or_404(payload.episode_id)
        route = episode.bootstrap.destination_routes.get(payload.destination_alias)
        if route is None or "http" not in route.protocols:
            raise HTTPException(status_code=403, detail="HTTP destination is not allowed")
        try:
            operation = _operation_for_http(route, payload.method, payload.path)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        allowed_headers = {name.lower() for name in route.allowed_headers}
        arguments = dict(payload.body)
        arguments["headers"] = {
            key: value for key, value in payload.headers.items() if key.lower() in allowed_headers
        }
        effect = normalize_http_effect(
            episode_id=payload.episode_id,
            destination_alias=payload.destination_alias,
            method=payload.method,
            path=payload.path,
            body={"operation": operation, "arguments": arguments},
            identity_alias=None,
            correlation_id=payload.correlation_id,
        )
        return await execute(effect, x_intervention_id)

    @app.post("/v1/mcp/tools/call", response_model=PublicEffectResponse)
    async def mcp_call(
        payload: McpToolCall,
        authorization: str | None = Header(default=None),
        x_intervention_id: str | None = Header(default=None),
    ) -> PublicEffectResponse:
        authorize_target(authorization, payload.episode_id, payload.destination_alias)
        effect = normalize_mcp_effect(
            episode_id=payload.episode_id,
            destination_alias=payload.destination_alias,
            tool_name=payload.tool_name,
            arguments=payload.arguments,
            identity_alias=None,
            correlation_id=payload.correlation_id,
        )
        return await execute(effect, x_intervention_id)

    @app.get("/v1/mcp/tools/list")
    def mcp_list(
        episode_id: str,
        destination_alias: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize_target(authorization, episode_id, destination_alias)
        episode = episode_or_404(episode_id)
        route = episode.bootstrap.destination_routes.get(destination_alias)
        if route is None or "mcp" not in route.protocols:
            raise HTTPException(status_code=403, detail="MCP destination is not allowed")
        return {"tools": [{"name": operation} for operation in route.operations]}

    @app.post("/mcp/{episode_id}/{destination_alias}")
    async def mcp_jsonrpc(
        episode_id: str,
        destination_alias: str,
        request: Request,
        authorization: str | None = Header(default=None),
        x_intervention_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize_target(authorization, episode_id, destination_alias)
        message = await request.json()
        request_id = message.get("id")
        method = message.get("method")
        episode = episode_or_404(episode_id)
        route = episode.bootstrap.destination_routes.get(destination_alias)
        if route is None or "mcp" not in route.protocols:
            raise HTTPException(status_code=403, detail="MCP destination is not allowed")
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "trusted-blue", "version": "0.2.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": operation, "inputSchema": {"type": "object"}}
                    for operation in route.operations
                ]
            }
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "tools/call":
            params = message.get("params") or {}
            effect = normalize_mcp_effect(
                episode_id=episode_id,
                destination_alias=destination_alias,
                tool_name=str(params.get("name", "")),
                arguments=dict(params.get("arguments") or {}),
                identity_alias=None,
                correlation_id=str(request_id),
            )
            public = await execute(effect, x_intervention_id)
            result = {
                "content": [{"type": "text", "text": json.dumps(public.result, sort_keys=True)}],
                "structuredContent": public.result,
                "isError": not public.success,
            }
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "method not found"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    instrument_fastapi(app)
    return app
