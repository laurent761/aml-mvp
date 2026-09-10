from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from .types import API_VERSION, EpisodeResult, Operation, SessionInfo


class ResearchAPIError(RuntimeError):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"AML API {status}: {detail}")
        self.status, self.detail = status, detail


class OperationTimeout(TimeoutError):
    def __init__(self, operation_id: str):
        super().__init__(f"operation {operation_id} is still pending; recover it with client.operation()")
        self.operation_id = operation_id


class IndeterminateOperation(ResearchAPIError):
    def __init__(self, operation: Operation):
        super().__init__(409, "operation outcome is indeterminate; retire this episode instead of retrying its action")
        self.operation = operation


class Client:
    def __init__(self, base_url: str, token: str, *, concurrency: int = 4, retries: int = 2,
                 timeout: float = 30, operation_timeout: float = 600,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not 1 <= concurrency <= 64 or not 0 <= retries <= 5 or timeout <= 0 or operation_timeout <= 0:
            raise ValueError("invalid client bounds")
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "X-AML-API-Version": API_VERSION},
            trust_env=False, follow_redirects=False, transport=transport)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.retries, self.operation_timeout = retries, operation_timeout

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, body: Any = None, *, key: str | None = None) -> Any:
        # Retries preserve the original key and JSON bytes. No POST is retried without a key.
        headers = {"Idempotency-Key": key} if key else {}
        method = method.upper()
        request = self.http.build_request(method, path, json=body, headers=headers)
        attempts = self.retries + 1 if method == "GET" or key else 1
        for attempt in range(attempts):
            try:
                response = await self.http.send(request)
                if response.status_code in {429, 502, 503, 504} and attempt + 1 < attempts:
                    await asyncio.sleep(min(2.0, 0.25 * 2 ** attempt))
                    continue
                if response.is_error:
                    try:
                        error_body = response.json()
                        detail = error_body.get("detail", error_body) if isinstance(error_body, dict) else error_body
                    except ValueError:
                        detail = "non-JSON error response"
                    raise ResearchAPIError(response.status_code, detail)
                return response.json()
            except httpx.TransportError:
                if attempt + 1 == attempts:
                    raise
                await asyncio.sleep(min(2.0, 0.25 * 2 ** attempt))

    async def catalog(self) -> list[dict[str, Any]]:
        return (await self.request("GET", "/v1/research-catalog"))["items"]

    async def create_session(self, *, key: str | None = None, **configuration: Any) -> EpisodeSession:
        data = await self.request("POST", "/v1/research-sessions", configuration, key=key or uuid.uuid4().hex)
        return EpisodeSession(self, SessionInfo.model_validate(data))

    async def attach_session(self, session_id: str) -> EpisodeSession:
        return EpisodeSession(self, SessionInfo.model_validate(await self.request("GET", f"/v1/research-sessions/{session_id}")))

    @asynccontextmanager
    async def session(self, **configuration: Any) -> AsyncIterator[EpisodeSession]:
        async with self.semaphore:
            episode = await self.create_session(**configuration)
            async with episode:
                yield episode

    async def operation(self, operation_id: str, *, wait: bool = True) -> Operation:
        deadline = time.monotonic() + self.operation_timeout
        while True:
            operation = Operation.model_validate(await self.request("GET", f"/v1/research-operations/{operation_id}"))
            if operation.status == "indeterminate":
                raise IndeterminateOperation(operation)
            if operation.status in {"failed", "cancelled"}:
                raise ResearchAPIError(409, operation.model_dump())
            if operation.status == "completed" or not wait:
                return operation
            if time.monotonic() >= deadline:
                raise OperationTimeout(operation_id)
            await asyncio.sleep(0.2)

    async def create_run(self, *, key: str | None = None, **document: Any) -> dict[str, Any]:
        return await self.request("POST", "/v1/research-runs", document, key=key or uuid.uuid4().hex)

    async def report_event(self, run_id: str, **event: Any) -> dict[str, Any]:
        event.setdefault("event_id", uuid.uuid4().hex)
        return await self.request("POST", f"/v1/research-runs/{run_id}/events", event, key=event["event_id"])

    async def record_generation(self, run_id: str, **generation: Any) -> dict[str, Any]:
        generation.setdefault("generation_id", uuid.uuid4().hex)
        return await self.request("POST", f"/v1/research-runs/{run_id}/generations", generation, key=generation["generation_id"])

    async def report_reward(self, run_id: str, **annotation: Any) -> dict[str, Any]:
        annotation.setdefault("annotation_id", uuid.uuid4().hex)
        return await self.request("POST", f"/v1/research-runs/{run_id}/rewards", annotation, key=annotation["annotation_id"])

    async def export_dataset(self, *, key: str | None = None, **filters: Any) -> dict[str, Any]:
        return await self.request("POST", "/v1/dataset-snapshots", filters, key=key or uuid.uuid4().hex)

    async def dataset(self, dataset_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/dataset-snapshots/{dataset_id}")

    async def upload(self, path: str | Path, *, run_id: str | None = None, key: str | None = None) -> dict[str, Any]:
        path = Path(path)
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        upload = await self.request("POST", "/v1/artifact-uploads", {"name": path.name,
            "size_bytes": path.stat().st_size, "sha256": digest.hexdigest(), "run_id": run_id}, key=key or uuid.uuid4().hex)
        if upload["status"] == "completed":
            return upload
        if upload["status"] != "uploaded":
            async def chunks() -> AsyncIterator[bytes]:
                with path.open("rb") as source:
                    while chunk := await asyncio.to_thread(source.read, 1024 * 1024):
                        yield chunk
            response = await self.http.put(f'/v1/artifact-uploads/{upload["id"]}/data', content=chunks(),
                headers={"Content-Type": "application/octet-stream"})
            if response.is_error:
                raise ResearchAPIError(response.status_code, response.text)
        return await self.request("POST", f'/v1/artifact-uploads/{upload["id"]}/complete', {}, key=upload["id"])

    async def download(self, artifact_id: str, destination: str | Path, *, sha256: str) -> Path:
        path = Path(destination)
        partial = path.with_name(path.name + ".partial-" + uuid.uuid4().hex)
        digest = hashlib.sha256()
        try:
            async with self.http.stream("GET", f"/v1/research-artifacts/{artifact_id}/download") as response:
                if response.is_error:
                    raise ResearchAPIError(response.status_code, "artifact download failed")
                with partial.open("xb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        digest.update(chunk)
                        await asyncio.to_thread(output.write, chunk)
            if digest.hexdigest() != sha256:
                raise ValueError("download checksum mismatch")
            partial.replace(path)
            return path
        finally:
            partial.unlink(missing_ok=True)

    async def register_checkpoint(self, *, key: str | None = None, **manifest: Any) -> dict[str, Any]:
        return await self.request("POST", "/v1/checkpoints", manifest, key=key or uuid.uuid4().hex)

    async def checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/checkpoints/{checkpoint_id}")

    async def register_runtime(self, **runtime: Any) -> dict[str, Any]:
        return await self.request("POST", "/v1/model-runtimes", runtime, key=uuid.uuid4().hex)

    async def submit_evaluation(self, *, key: str | None = None, **configuration: Any) -> dict[str, Any]:
        return await self.request("POST", "/v1/evaluations", configuration, key=key or uuid.uuid4().hex)

    async def evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/evaluations/{evaluation_id}")

    async def reproduce(self, episode_id: str, *, key: str | None = None) -> EpisodeSession:
        data = await self.request("POST", f"/v1/research-episodes/{episode_id}/reproduce", {}, key=key or uuid.uuid4().hex)
        return EpisodeSession(self, SessionInfo.model_validate(data))


class EpisodeSession:
    def __init__(self, client: Client, info: SessionInfo):
        self.client, self.info = client, info
        self.last_operation_id: str | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        # Serialize snapshots without blocking heartbeats on long-running commands.
        self._status_lock = asyncio.Lock()

    @property
    def id(self) -> str:
        return self.info.id

    async def __aenter__(self) -> EpisodeSession:
        async def maintain() -> None:
            while True:
                await asyncio.sleep(self.info.configuration["limits"]["heartbeat_seconds"] / 3)
                await self.heartbeat()
        self._heartbeat = asyncio.create_task(maintain())
        return self

    async def __aexit__(self, *args: Any) -> None:
        try:
            if self._heartbeat:
                self._heartbeat.cancel()
                try:
                    await self._heartbeat
                except asyncio.CancelledError:
                    pass
        finally:
            await asyncio.shield(self.close())

    async def status(self) -> SessionInfo:
        async with self._status_lock:
            self.info = SessionInfo.model_validate(await self.client.request("GET", f"/v1/research-sessions/{self.id}"))
            return self.info

    async def heartbeat(self) -> SessionInfo:
        async with self._status_lock:
            self.info = SessionInfo.model_validate(await self.client.request("POST", f"/v1/research-sessions/{self.id}/heartbeat", {}))
            return self.info

    async def command(self, kind: str, payload: Any, key: str | None) -> Operation:
        data = await self.client.request("POST", f"/v1/research-sessions/{self.id}/{kind}", payload, key=key or uuid.uuid4().hex)
        self.last_operation_id = data["id"]
        return await self.client.operation(data["id"])

    async def reset(self, *, seed: int | None = None, key: str | None = None) -> EpisodeResult:
        async with self._lock:
            operation = await self.command("reset", {"seed": seed}, key)
            result = EpisodeResult.model_validate(operation.result)
            await self.status()
            return result

    async def step(self, action: dict[str, Any], *, generation_id: str | None = None,
                   provenance: str = "generated", key: str | None = None) -> EpisodeResult:
        async with self._lock:
            action = dict(action)
            action.setdefault("action_id", uuid.uuid4().hex)
            operation = await self.command("steps", {"episode_id": self.info.episode_id,
                "expected_step_index": self.info.step_index + 1, "action": action,
                "generation_id": generation_id, "provenance": provenance}, key)
            result = EpisodeResult.model_validate(operation.result)
            await self.status()
            return result

    async def trajectory(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return (await self.client.request("GET", f"/v1/research-sessions/{self.id}/trajectory?limit={limit}&offset={offset}"))["items"]

    async def cancel(self, *, key: str | None = None) -> Operation:
        return await self.command("cancel", {}, key)

    async def close(self, *, key: str | None = None) -> Operation:
        return await self.command("close", {}, key or "sdk-close")
