from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status

from .capsule import (
    CapsuleError,
    CapsuleRuntime,
    DockerCapsuleRuntime,
    ReconciliationResult,
)
from .contracts import CapsuleHandle, CapsuleSpec
from .settings import Settings, get_settings
from .telemetry import configure_telemetry, instrument_fastapi


class CapsuleSupervisorClient:
    """Authenticated client implementing CapsuleRuntime over the supervisor HTTP contract."""

    def __init__(
        self,
        base_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._headers = {"Authorization": f"Bearer {token}"}
        self.client = client or httpx.AsyncClient(timeout=120, trust_env=False)
        self._owns_client = client is None

    async def create(self, spec: CapsuleSpec) -> CapsuleHandle:
        response = await self.client.post(
            f"{self.base_url}/v1/capsules",
            headers=self._headers,
            json=spec.model_dump(mode="json"),
        )
        self._raise_for_status(response)
        handle = CapsuleHandle.model_validate(response.json())
        return handle.model_copy(
            update={
                "gateway_ingress_url": (f"{self.base_url}/v1/capsules/{handle.capsule_id}/gateway"),
                "supervisor_capability": self._token,
                "gateway_auth_header": "Authorization",
            }
        )

    async def healthcheck(self, handle: CapsuleHandle) -> bool:
        response = await self.client.get(
            f"{self.base_url}/v1/capsules/{handle.capsule_id}/health",
            headers=self._headers,
        )
        if response.status_code == status.HTTP_404_NOT_FOUND:
            response = await self.client.get(
                f"{self.base_url}/v1/capsules/{handle.capsule_id}/ready",
                headers=self._headers,
            )
        self._raise_for_status(response)
        document = response.json()
        return bool(document.get("healthy", document.get("ready", False)))

    async def reset(self, handle: CapsuleHandle) -> None:
        response = await self.client.post(
            f"{self.base_url}/v1/capsules/{handle.capsule_id}/reset",
            headers=self._headers,
        )
        self._raise_for_status(response)

    async def destroy(self, handle: CapsuleHandle) -> None:
        response = await self.client.delete(
            f"{self.base_url}/v1/capsules/{handle.capsule_id}",
            headers=self._headers,
        )
        if response.status_code == status.HTTP_404_NOT_FOUND:
            return
        self._raise_for_status(response)

    async def gateway_request(
        self,
        handle: CapsuleHandle,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: Any = None,
        json_body: Any = None,
    ) -> httpx.Response:
        safe_path = path.lstrip("/")
        request_headers = dict(headers or {})
        request_headers.update(self._headers)
        response = await self.client.request(
            method,
            f"{self.base_url}/v1/capsules/{handle.capsule_id}/gateway/{safe_path}",
            headers=request_headers,
            params=params,
            json=json_body,
            timeout=handle.target_timeout_seconds + 15
            if path.rstrip("/").endswith(("/invoke", "/reset"))
            else 120,
        )
        return response

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                detail = str(response.json().get("detail", "capsule supervisor request failed"))
            except (ValueError, AttributeError):
                detail = "capsule supervisor request failed"
            raise CapsuleError(detail) from exc


class SupervisorState:
    def __init__(self, runtime: CapsuleRuntime, max_concurrency: int) -> None:
        self.runtime = runtime
        self.handles: dict[str, CapsuleHandle] = {}
        self._lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(max_concurrency)
        self.reconciliation = ReconciliationResult(0, 0, 0, 0)

    async def reconcile_startup(self) -> ReconciliationResult:
        reconcile = getattr(self.runtime, "reconcile_orphans", None)
        if reconcile is None:
            return self.reconciliation
        async with self._lock:
            active = set(self.handles)
        result = await reconcile(active)
        self.reconciliation = result
        return result

    async def create(self, spec: CapsuleSpec) -> CapsuleHandle:
        await self._capacity.acquire()
        try:
            handle = await self.runtime.create(spec)
            if handle.episode_id != spec.episode_id:
                await self.runtime.destroy(handle)
                raise CapsuleError("runtime returned a handle for the wrong episode")
            async with self._lock:
                self.handles[handle.capsule_id] = handle
            return handle
        except BaseException:
            self._capacity.release()
            raise

    async def get(self, capsule_id: str) -> CapsuleHandle:
        async with self._lock:
            handle = self.handles.get(capsule_id)
        if handle is None:
            raise KeyError("capsule not found")
        return handle

    async def destroy(self, capsule_id: str) -> None:
        handle = await self.get(capsule_id)
        await self.runtime.destroy(handle)
        async with self._lock:
            removed = self.handles.pop(capsule_id, None)
        if removed is not None:
            self._capacity.release()

    async def close(self) -> None:
        async with self._lock:
            capsule_ids = list(self.handles)
        for capsule_id in capsule_ids:
            try:
                await self.destroy(capsule_id)
            except Exception:
                continue
        close = getattr(self.runtime, "aclose", None)
        if close is not None:
            await close()

    async def reap_expired(self) -> None:
        """Independent of workers: abandoned capsules have a bounded lifetime."""
        from datetime import UTC, datetime
        while True:
            await asyncio.sleep(5)
            async with self._lock:
                expired = [handle.capsule_id for handle in self.handles.values()
                    if (datetime.now(UTC) - handle.created_at).total_seconds() > handle.target_timeout_seconds + 60]
            for capsule_id in expired:
                try:
                    await self.destroy(capsule_id)
                except Exception:
                    continue


def build_supervised_runtime(settings: Settings) -> CapsuleRuntime:
    if settings.capsule_runtime == "docker":
        from .inference import TargetInferenceBroker

        return DockerCapsuleRuntime(
            blue_image=settings.blue_gateway_image,
            capability_signing_key=settings.capability_signing_key,
            inference_broker=TargetInferenceBroker(settings)
            if settings.target_model_provider != "disabled"
            else None,
        )
    if not settings.firecracker_runner_url or not settings.firecracker_runner_token:
        raise RuntimeError("Firecracker remote runner configuration is incomplete")
    return CapsuleSupervisorClient(
        settings.firecracker_runner_url,
        settings.firecracker_runner_token,
    )


def create_supervisor_app(
    settings: Settings | None = None,
    runtime: CapsuleRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    telemetry = configure_telemetry(settings, service_name="capsule-supervisor")
    state = SupervisorState(
        runtime or build_supervised_runtime(settings),
        settings.max_capsule_concurrency,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        reaper: asyncio.Task[None] | None = None
        try:
            await state.reconcile_startup()
            reaper = asyncio.create_task(state.reap_expired())
            yield
        finally:
            if reaper:
                reaper.cancel()
                try:
                    await reaper
                except asyncio.CancelledError:
                    pass
            await state.close()
            telemetry.shutdown()

    app = FastAPI(title="Capsule Supervisor", version="0.1.0", lifespan=lifespan)
    app.state.supervisor = state

    def authorize(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {settings.capsule_supervisor_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="valid supervisor token required")

    def translate_runtime_error(exc: Exception) -> HTTPException:
        if isinstance(exc, KeyError):
            return HTTPException(status_code=404, detail="capsule not found")
        return HTTPException(status_code=409, detail=str(exc))

    def public_handle(handle: CapsuleHandle) -> CapsuleHandle:
        return handle.model_copy(
            update={
                "gateway_ingress_url": None,
                "supervisor_capability": None,
            }
        )

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "orphan_reconciliation": {
                "discovered": state.reconciliation.discovered,
                "orphaned": state.reconciliation.orphaned,
                "removed": state.reconciliation.removed,
                "active_capsules": state.reconciliation.active_capsules,
            },
        }

    @app.post(
        "/v1/capsules",
        response_model=CapsuleHandle,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(authorize)],
    )
    async def create_capsule(spec: CapsuleSpec) -> CapsuleHandle:
        try:
            return public_handle(await state.create(spec))
        except Exception as exc:
            raise translate_runtime_error(exc) from exc

    @app.get(
        "/v1/capsules/{capsule_id}/health",
        dependencies=[Depends(authorize)],
    )
    async def capsule_health(capsule_id: str) -> dict[str, Any]:
        try:
            handle = await state.get(capsule_id)
            return {"capsule_id": capsule_id, "healthy": await state.runtime.healthcheck(handle)}
        except Exception as exc:
            raise translate_runtime_error(exc) from exc

    @app.get(
        "/v1/capsules/{capsule_id}/ready",
        dependencies=[Depends(authorize)],
    )
    async def capsule_ready(capsule_id: str) -> dict[str, Any]:
        try:
            handle = await state.get(capsule_id)
            return {"capsule_id": capsule_id, "ready": await state.runtime.healthcheck(handle)}
        except Exception as exc:
            raise translate_runtime_error(exc) from exc

    @app.api_route(
        "/v1/capsules/{capsule_id}/gateway/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        dependencies=[Depends(authorize)],
    )
    async def gateway_proxy(capsule_id: str, path: str, request: Request) -> Response:
        gateway_request = getattr(state.runtime, "gateway_request", None)
        if gateway_request is None:
            raise HTTPException(status_code=501, detail="runtime gateway proxy is unavailable")
        try:
            handle = await state.get(capsule_id)
            body = await request.body()
            json_body: Any = None
            if body:
                content_type = request.headers.get("content-type", "")
                if "application/json" not in content_type:
                    raise HTTPException(status_code=415, detail="gateway proxy accepts JSON only")
                try:
                    json_body = await request.json()
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="invalid JSON body") from exc
            forwarded_headers = {
                name: value
                for name, value in request.headers.items()
                if name.lower()
                in {
                    "accept",
                    "content-type",
                    "idempotency-key",
                    "x-correlation-id",
                    "x-request-id",
                }
            }
            upstream = await gateway_request(
                handle,
                request.method,
                "/" + path.lstrip("/"),
                headers=forwarded_headers,
                params=list(request.query_params.multi_items()),
                json_body=json_body,
            )
            response_headers = {
                name: value
                for name, value in upstream.headers.items()
                if name.lower() in {"content-type", "etag", "x-correlation-id", "x-request-id"}
            }
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=response_headers,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise translate_runtime_error(exc) from exc

    @app.post(
        "/v1/capsules/{capsule_id}/reset",
        dependencies=[Depends(authorize)],
    )
    async def reset_capsule(capsule_id: str) -> dict[str, str]:
        try:
            handle = await state.get(capsule_id)
            await state.runtime.reset(handle)
            return {"capsule_id": capsule_id, "status": "reset"}
        except Exception as exc:
            raise translate_runtime_error(exc) from exc

    @app.delete(
        "/v1/capsules/{capsule_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(authorize)],
    )
    async def destroy_capsule(capsule_id: str) -> Response:
        try:
            await state.destroy(capsule_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        except Exception as exc:
            raise translate_runtime_error(exc) from exc

    instrument_fastapi(app)
    return app
