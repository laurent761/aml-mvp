from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

import httpx

from .contracts import (
    CapsuleHandle,
    CapsuleSpec,
    ContainmentResult,
    RuntimeContainmentProof,
    new_id,
)
from .image_readiness import ImageReadiness, ImageUnavailable, inspect_image
from .inference import TargetInferenceBroker
from .inference_contracts import INFERENCE_DESTINATION, InferenceWork
from .security import CapabilityTokenService, redact_sensitive


class CapsuleError(RuntimeError):
    pass


class CapsuleRuntime(Protocol):
    async def create(self, spec: CapsuleSpec) -> CapsuleHandle: ...
    async def healthcheck(self, handle: CapsuleHandle) -> bool: ...
    async def reset(self, handle: CapsuleHandle) -> None: ...
    async def destroy(self, handle: CapsuleHandle) -> None: ...


MANAGED_LABEL = "io.adversarial-agent-mvp.capsule.managed"
CAPSULE_ID_LABEL = "io.adversarial-agent-mvp.capsule.id"
EPISODE_ID_LABEL = "io.adversarial-agent-mvp.episode.id"
RESOURCE_ROLE_LABEL = "io.adversarial-agent-mvp.resource.role"


@dataclass(frozen=True)
class RuntimeResource:
    kind: Literal["container", "network"]
    resource_id: str
    name: str
    capsule_id: str | None
    episode_id: str | None
    role: str | None


@dataclass(frozen=True)
class ReconciliationResult:
    discovered: int
    orphaned: int
    removed: int
    active_capsules: int


_SECRET_NAME = re.compile(r"(secret|token|password|credential|api[_-]?key)", re.I)
_REAL_DESTINATION = re.compile(r"^(https?://|[0-9]{1,3}(?:\.[0-9]{1,3}){3})")
_BLUE_EXEC_PROXY = """
import base64, json, sys, urllib.error, urllib.request
p = json.loads(base64.urlsafe_b64decode(sys.argv[1]).decode())
data = None if p["json"] is None else json.dumps(p["json"]).encode()
headers = dict(p["headers"])
if data is not None:
    headers.setdefault("Content-Type", "application/json")
request = urllib.request.Request(
    "http://127.0.0.1:8080" + p["path"],
    data=data,
    headers=headers,
    method=p["method"],
)
try:
    response = urllib.request.urlopen(request, timeout=p.get("timeout", 30))
except urllib.error.HTTPError as error:
    response = error
body = response.read()
print(json.dumps({
    "status": response.status,
    "headers": dict(response.headers.items()),
    "body": base64.b64encode(body).decode(),
}))
""".strip()


def containment_preflight(spec: CapsuleSpec) -> ContainmentResult:
    violations: list[str] = []
    if spec.privileged:
        violations.append("privileged mode is forbidden")
    if spec.host_network:
        violations.append("host network is forbidden")
    if spec.host_mounts:
        violations.append("host mounts are forbidden")
    if spec.docker_socket:
        violations.append("Docker socket is forbidden")
    if spec.external_dns:
        violations.append("external DNS is forbidden")
    if any(_SECRET_NAME.search(name) for name in spec.environment):
        violations.append("real or secret-like environment values are forbidden")
    if any(_REAL_DESTINATION.search(alias) for alias in spec.allowed_destination_aliases):
        violations.append("destinations must be opaque virtual aliases")
    declared = set(spec.destination_routes)
    allowed = set(spec.allowed_destination_aliases)
    if declared and declared != allowed:
        violations.append("destination routes must exactly match allowed aliases")
    if any(alias not in allowed for alias in spec.environment_aliases.values()):
        violations.append("environment aliases must reference allowed destinations")
    if INFERENCE_DESTINATION in allowed or INFERENCE_DESTINATION in declared:
        violations.append("inference cannot be a virtual business destination")
    return ContainmentResult(verified=not violations, violations=violations)


class DockerCommandRunner:
    async def run(self, *args: str, timeout: float = 30) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise CapsuleError("Docker operation timed out") from None
        if process.returncode:
            raise CapsuleError(stderr.decode(errors="replace").strip() or "Docker operation failed")
        return stdout.decode().strip()


class GatewayControlClient:
    async def wait_ready(self, ingress_url: str, timeout: float = 30) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            while True:
                try:
                    response = await client.get(f"{ingress_url}/healthz")
                    if response.is_success:
                        return
                except httpx.HTTPError:
                    pass
                if asyncio.get_running_loop().time() >= deadline:
                    raise CapsuleError("Blue gateway readiness timed out")
                await asyncio.sleep(0.1)

    async def episode_ready(self, handle: CapsuleHandle) -> bool:
        if not handle.gateway_ingress_url or not handle.supervisor_capability:
            return False
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(
                    f"{handle.gateway_ingress_url}/v1/admin/episodes/{handle.episode_id}/ready",
                    headers={"X-Blue-Supervisor": handle.supervisor_capability},
                )
            return response.is_success and bool(response.json().get("ready"))
        except (httpx.HTTPError, ValueError):
            return False

    async def reset(self, handle: CapsuleHandle) -> None:
        if not handle.gateway_ingress_url or not handle.supervisor_capability:
            raise CapsuleError("capsule has no trusted gateway ingress")
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(
                f"{handle.gateway_ingress_url}/v1/admin/episodes/{handle.episode_id}/reset",
                headers={"X-Blue-Supervisor": handle.supervisor_capability},
            )
        if not response.is_success:
            raise CapsuleError("capsule reset failed")

    async def teardown(self, handle: CapsuleHandle) -> None:
        if not handle.gateway_ingress_url or not handle.supervisor_capability:
            return
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                await client.delete(
                    f"{handle.gateway_ingress_url}/v1/admin/episodes/{handle.episode_id}",
                    headers={"X-Blue-Supervisor": handle.supervisor_capability},
                )
        except httpx.HTTPError:
            return


class DockerCapsuleRuntime:
    """One internal target/Blue network plus localhost-only trusted ingress."""

    def __init__(
        self,
        runner: DockerCommandRunner | None = None,
        blue_image: str = "blue-gateway:local",
        *,
        capability_signing_key: str | None = None,
        control_client: GatewayControlClient | None = None,
        inference_broker: TargetInferenceBroker | None = None,
    ):
        self.runner = runner or DockerCommandRunner()
        self.blue_image = blue_image
        self.capability_signing_key = capability_signing_key or secrets.token_urlsafe(48)
        self.control_client = control_client or GatewayControlClient()
        self.inference_broker = inference_broker
        self._inference_relays: dict[str, asyncio.Task[None]] = {}

    async def image_readiness(self, image: str, *, restore: bool = False) -> ImageReadiness:
        return await inspect_image(self.runner, image, restore=restore)

    async def prepare_image(self, image: str) -> ImageReadiness:
        result = await self.image_readiness(image, restore=True)
        if result.status != "READY":
            raise ImageUnavailable(result)
        return result

    async def create(self, spec: CapsuleSpec) -> CapsuleHandle:
        preflight = containment_preflight(spec)
        if not preflight.verified:
            raise CapsuleError("containment preflight rejected: " + "; ".join(preflight.violations))
        if spec.inference_profile:
            if self.inference_broker is None:
                raise CapsuleError("target inference is not configured in the supervisor")
            self.inference_broker.register(spec.episode_id, spec.inference_profile)
        capsule_id, network_name = new_id("capsule"), new_id("capsule_net")
        blue_name, target_name = f"{capsule_id}-blue", f"{capsule_id}-target"
        supervisor_capability = secrets.token_urlsafe(48)
        target_capability = CapabilityTokenService(self.capability_signing_key).issue(
            spec.episode_id,
            spec.allowed_destination_aliases,
            ttl_seconds=max(spec.resource_limits.timeout_seconds + 60, 300),
        )
        inference_capability = (
            CapabilityTokenService(self.capability_signing_key).issue(
                spec.episode_id,
                [INFERENCE_DESTINATION],
                ttl_seconds=max(spec.resource_limits.timeout_seconds + 60, 300),
            )
            if spec.inference_profile
            else None
        )
        created: list[tuple[str, str]] = []
        sensitive_values = [
            *spec.environment.values(),
            self.capability_signing_key,
            supervisor_capability,
            target_capability,
            inference_capability or "",
        ]
        handle: CapsuleHandle | None = None
        try:
            await self.prepare_image(spec.image)
            ownership_labels = self._ownership_labels(capsule_id, spec.episode_id)
            network_id = await self.runner.run(
                "network",
                "create",
                "--internal",
                *self._label_arguments({**ownership_labels, RESOURCE_ROLE_LABEL: "network"}),
                network_name,
            )
            created.append(("network", network_name))
            blue_id = await self.runner.run(
                "run",
                "-d",
                "--name",
                blue_name,
                *self._label_arguments({**ownership_labels, RESOURCE_ROLE_LABEL: "blue"}),
                "--network",
                network_name,
                "--network-alias",
                "blue",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "128",
                "--memory",
                "256m",
                "--cpus",
                "0.5",
                "--env",
                f"CAPABILITY_SIGNING_KEY={self.capability_signing_key}",
                "--env",
                f"BLUE_SUPERVISOR_TOKEN={supervisor_capability}",
                self.blue_image,
                "adversarial-blue-gateway",
            )
            created.append(("container", blue_id or blue_name))
            partial_handle = CapsuleHandle(
                capsule_id=capsule_id,
                episode_id=spec.episode_id,
                target_container_id="pending",
                blue_container_id=blue_id,
                blue_container_name=blue_name,
                network_id=network_id,
                network_name=network_name,
                blue_alias="blue",
                supervisor_capability=supervisor_capability,
            )
            await self._wait_blue_ready(partial_handle)

            target_environment = dict(spec.environment)
            target_environment.update(
                {
                    "BLUE_GATEWAY_URL": "http://blue:8080",
                    "BLUE_CAPABILITY_TOKEN": target_capability,
                    "BLUE_EPISODE_ID": spec.episode_id,
                }
            )
            if inference_capability:
                target_environment["BLUE_INFERENCE_CAPABILITY"] = inference_capability
            for name, alias in spec.environment_aliases.items():
                route = spec.destination_routes[alias]
                if "http" in route.protocols:
                    target_environment[name] = f"http://blue:8080/http/{spec.episode_id}/{alias}"
                else:
                    target_environment[name] = f"http://blue:8080/mcp/{spec.episode_id}/{alias}"
            command = [
                "run",
                "-d",
                "--name",
                target_name,
                *self._label_arguments({**ownership_labels, RESOURCE_ROLE_LABEL: "target"}),
                "--network",
                network_name,
                "--network-alias",
                "target",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                str(spec.resource_limits.pids_limit),
                "--memory",
                f"{spec.resource_limits.memory_mb}m",
                "--cpus",
                str(spec.resource_limits.cpu_count),
            ]
            for name, value in target_environment.items():
                command.extend(["--env", f"{name}={value}"])
            # The manifest field is an OCI entrypoint, not additional CMD
            # arguments. Force it explicitly so an image-provided ENTRYPOINT
            # cannot intercept or change the configured startup executable.
            command.extend(["--entrypoint", spec.entrypoint[0], spec.image, *spec.entrypoint[1:]])
            target_id = await self.runner.run(
                *command,
                timeout=spec.resource_limits.timeout_seconds,
            )
            created.append(("container", target_id or target_name))
            handle = CapsuleHandle(
                capsule_id=capsule_id,
                episode_id=spec.episode_id,
                target_container_id=target_id,
                blue_container_id=blue_id,
                target_container_name=target_name,
                blue_container_name=blue_name,
                network_id=network_id,
                network_name=network_name,
                blue_alias="blue",
                supervisor_capability=supervisor_capability,
                inference_profile=spec.inference_profile,
                target_timeout_seconds=spec.resource_limits.timeout_seconds,
            )
            proof = await self.verify_network_boundary(handle)
            if spec.inference_profile:
                self._inference_relays[handle.capsule_id] = asyncio.create_task(
                    self._relay_inference(handle)
                )
            return handle.model_copy(update={"runtime_containment_proof": proof})
        except BaseException as exc:
            if spec.inference_profile and self.inference_broker:
                await self.inference_broker.remove(spec.episode_id)
            if handle is not None:
                try:
                    await asyncio.shield(
                        self.gateway_request(
                            handle,
                            "DELETE",
                            f"/v1/admin/episodes/{spec.episode_id}",
                        )
                    )
                except BaseException:
                    pass
            await self._cleanup_after_interruption(created)
            if isinstance(exc, (asyncio.CancelledError, ImageUnavailable)):
                raise
            if "no such image" in str(exc).lower():
                from .image_readiness import unavailable
                result = await self.image_readiness(spec.image)
                if result.status == "READY":
                    result = unavailable(spec.image, "RUNTIME_IMAGE_MISSING", "AML's execution environment is missing a required runtime image.", "Rebuild the AML runtime services, then start a new campaign.")
                raise ImageUnavailable(result) from None
            message = redact_sensitive(str(exc), sensitive_values)
            raise CapsuleError(message) from None

    async def verify_network_boundary(self, handle: CapsuleHandle) -> RuntimeContainmentProof:
        expected_network = handle.network_name or handle.network_id
        network_counts: dict[Literal["blue", "target"], int] = {"blue": 0, "target": 0}
        ownership_verified = True
        resources: tuple[
            tuple[Literal["target", "blue"], str | None],
            tuple[Literal["target", "blue"], str | None],
        ] = (
            ("target", handle.target_container_id),
            ("blue", handle.blue_container_id),
        )
        for role, container_id in resources:
            if not container_id:
                raise CapsuleError("capsule is missing a container identity")
            raw = await self.runner.run("inspect", container_id)
            info = json.loads(raw)[0]
            networks = info.get("NetworkSettings", {}).get("Networks", {})
            network_counts[role] = len(networks)
            if len(networks) != 1 or expected_network not in networks:
                raise CapsuleError("capsule container has an unexpected network attachment")
            labels = info.get("Config", {}).get("Labels") or {}
            if handle.inference_profile:
                environment = dict(
                    item.split("=", 1)
                    for item in info.get("Config", {}).get("Env", [])
                    if "=" in item
                )
                allowed_secrets = (
                    {"BLUE_CAPABILITY_TOKEN", "BLUE_INFERENCE_CAPABILITY"}
                    if role == "target"
                    else {"BLUE_SUPERVISOR_TOKEN"}
                )
                if any(
                    _SECRET_NAME.search(name) and name not in allowed_secrets
                    for name in environment
                ):
                    raise CapsuleError("inference container contains an unexpected credential")
                if role == "target":
                    capability = environment.get("BLUE_INFERENCE_CAPABILITY", "")
                    try:
                        claims = CapabilityTokenService(self.capability_signing_key).verify(
                            capability,
                            episode_id=handle.episode_id,
                            destination=INFERENCE_DESTINATION,
                        )
                        if claims["destinations"] != [INFERENCE_DESTINATION]:
                            raise ValueError("unexpected inference scope")
                    except ValueError as exc:
                        raise CapsuleError("target inference capability is invalid") from exc
            ownership_verified = ownership_verified and self._labels_match(
                labels,
                handle.capsule_id,
                handle.episode_id,
                role,
            )
        network_details = await self.runner.run("network", "inspect", handle.network_id)
        network = json.loads(network_details)[0]
        if not network.get("Internal"):
            raise CapsuleError("capsule network is not internal")
        ownership_verified = ownership_verified and self._labels_match(
            network.get("Labels") or {},
            handle.capsule_id,
            handle.episode_id,
            "network",
        )
        if not ownership_verified:
            raise CapsuleError("capsule resources have invalid ownership labels")
        members = set((network.get("Containers") or {}).keys())
        expected_members = {handle.target_container_id, handle.blue_container_id}
        if members != expected_members:
            raise CapsuleError("capsule network has unexpected members")
        fingerprint = hashlib.sha256(
            "\n".join(sorted({handle.network_id, *expected_members})).encode()
        ).hexdigest()
        return RuntimeContainmentProof(
            capsule_id=handle.capsule_id,
            episode_id=handle.episode_id,
            verified=True,
            network_internal=True,
            ownership_labels_verified=ownership_verified,
            container_network_counts=network_counts,
            actual_member_count=len(members),
            unexpected_attachment_count=0,
            resource_fingerprint=fingerprint,
            inference_transport="supervisor_queue" if handle.inference_profile else "none",
            inference_credentials_isolated=True if handle.inference_profile else None,
        )

    async def healthcheck(self, handle: CapsuleHandle) -> bool:
        if handle.inference_profile:
            relay = self._inference_relays.get(handle.capsule_id)
            if relay is None or relay.done():
                return False
        for container_id in (handle.target_container_id, handle.blue_container_id):
            if not container_id:
                return False
            output = await self.runner.run(
                "inspect", "--format", "{{.State.Running}}", container_id
            )
            if output.strip().lower() != "true":
                return False
        try:
            response = await self.gateway_request(
                handle,
                "GET",
                f"/v1/admin/episodes/{handle.episode_id}/ready",
            )
            return response.is_success and bool(response.json().get("ready"))
        except (CapsuleError, ValueError):
            return False

    async def reset(self, handle: CapsuleHandle) -> None:
        response = await self.gateway_request(
            handle,
            "POST",
            f"/v1/admin/episodes/{handle.episode_id}/reset",
        )
        if not response.is_success:
            raise CapsuleError("capsule reset failed")

    async def destroy(self, handle: CapsuleHandle) -> None:
        relay = self._inference_relays.pop(handle.capsule_id, None)
        if relay:
            relay.cancel()
        if handle.inference_profile and self.inference_broker:
            await self.inference_broker.remove(handle.episode_id)
        interrupted: asyncio.CancelledError | None = None
        try:
            if relay:
                await asyncio.gather(relay, return_exceptions=True)
            await self.gateway_request(
                handle,
                "DELETE",
                f"/v1/admin/episodes/{handle.episode_id}",
            )
        except asyncio.CancelledError as exc:
            interrupted = exc
        except Exception:
            pass
        resources = [
            ("container", handle.target_container_id),
            ("container", handle.blue_container_id or handle.blue_container_name or ""),
            ("network", handle.network_id),
        ]
        if interrupted is not None:
            await self._cleanup_after_interruption(resources)
            raise interrupted
        await self._cleanup_cancel_safe(resources, suppress=False)

    async def _relay_inference(self, handle: CapsuleHandle) -> None:
        try:
            await self._run_inference_relay(handle)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail closed without exposing provider errors or credentials.
            try:
                await self.gateway_request(
                    handle, "POST", f"/v1/admin/episodes/{handle.episode_id}/inference/fail"
                )
            except Exception:
                pass

    async def _run_inference_relay(self, handle: CapsuleHandle) -> None:
        assert self.inference_broker is not None
        failures = 0
        while True:
            try:
                response = await self.gateway_request(
                    handle, "POST", f"/v1/admin/episodes/{handle.episode_id}/inference/claim"
                )
            except CapsuleError:
                failures += 1
                if failures >= 100:
                    raise
                await asyncio.sleep(0.2)
                continue
            failures = 0
            if response.status_code == 404:
                await asyncio.sleep(0.2)  # Target bootstrap has not arrived yet.
                continue
            response.raise_for_status()
            requests = response.json().get("requests", [])
            for document in requests:
                work = InferenceWork.model_validate(document)
                if work.episode_id != handle.episode_id:
                    raise CapsuleError("inference relay received a different episode")
                result = await self.inference_broker.complete(work)
                for attempt in range(3):
                    try:
                        completion = await self.gateway_request(
                            handle,
                            "POST",
                            f"/v1/admin/episodes/{handle.episode_id}/inference/complete",
                            json_body=result.model_dump(mode="json"),
                        )
                        completion.raise_for_status()
                        break
                    except (CapsuleError, httpx.HTTPError):
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.2)
            if not requests:
                await asyncio.sleep(0.1)

    async def aclose(self) -> None:
        relays = list(self._inference_relays.values())
        self._inference_relays.clear()
        for relay in relays:
            relay.cancel()
        await asyncio.gather(*relays, return_exceptions=True)
        if self.inference_broker:
            await self.inference_broker.aclose()

    async def inventory(self) -> list[RuntimeResource]:
        """Inventory only resources carrying our explicit managed-resource label."""

        resources: list[RuntimeResource] = []
        container_ids = await self.runner.run(
            "ps",
            "-aq",
            "--filter",
            f"label={MANAGED_LABEL}=true",
        )
        for resource_id in container_ids.splitlines():
            resource = await self._inspect_resource("container", resource_id.strip())
            if resource is not None:
                resources.append(resource)
        network_ids = await self.runner.run(
            "network",
            "ls",
            "-q",
            "--filter",
            f"label={MANAGED_LABEL}=true",
        )
        for resource_id in network_ids.splitlines():
            resource = await self._inspect_resource("network", resource_id.strip())
            if resource is not None:
                resources.append(resource)
        return resources

    async def reconcile_orphans(
        self,
        active_capsule_ids: set[str] | None = None,
    ) -> ReconciliationResult:
        """Remove labeled resources not owned by an in-memory active capsule.

        A supervisor restart begins with an empty active set. The Docker endpoint is a
        dedicated supervisor boundary, so every previously managed resource is then an
        orphan. Missing resources during cleanup are treated as an already-completed
        idempotent removal.
        """

        active = set(active_capsule_ids or set())
        resources = await self.inventory()
        orphans = [item for item in resources if item.capsule_id not in active]
        removals = [(item.kind, item.resource_id) for item in orphans]
        await self._cleanup_cancel_safe(removals, suppress=False, ignore_missing=True)
        return ReconciliationResult(
            discovered=len(resources),
            orphaned=len(orphans),
            removed=len(orphans),
            active_capsules=len(active),
        )

    async def _cleanup(
        self,
        resources: list[tuple[str, str]],
        *,
        suppress: bool,
        ignore_missing: bool = False,
    ) -> None:
        failures: list[str] = []
        ordered = [item for item in resources if item[0] == "container" and item[1]]
        ordered.extend(item for item in resources if item[0] == "network" and item[1])
        for kind, identifier in ordered:
            try:
                if kind == "container":
                    await self.runner.run("rm", "-f", identifier)
                else:
                    await self.runner.run("network", "rm", identifier)
            except Exception as exc:
                if ignore_missing and self._is_missing_resource_error(exc):
                    continue
                failures.append(f"{kind} {identifier}: {exc}")
        if failures and not suppress:
            raise CapsuleError("capsule cleanup incomplete: " + "; ".join(failures))

    async def _cleanup_cancel_safe(
        self,
        resources: list[tuple[str, str]],
        *,
        suppress: bool,
        ignore_missing: bool = False,
    ) -> None:
        cleanup = asyncio.create_task(
            self._cleanup(
                resources,
                suppress=suppress,
                ignore_missing=ignore_missing,
            )
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                await cleanup
            except Exception:
                pass
            raise

    async def _cleanup_after_interruption(self, resources: list[tuple[str, str]]) -> None:
        cleanup = asyncio.create_task(self._cleanup(resources, suppress=True))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                await cleanup
            except Exception:
                pass

    async def _inspect_resource(
        self,
        kind: Literal["container", "network"],
        resource_id: str,
    ) -> RuntimeResource | None:
        if not resource_id:
            return None
        try:
            raw = await self.runner.run(
                *("inspect", resource_id)
                if kind == "container"
                else ("network", "inspect", resource_id)
            )
        except Exception as exc:
            if self._is_missing_resource_error(exc):
                return None
            raise
        document = json.loads(raw)[0]
        labels = (
            document.get("Config", {}).get("Labels")
            if kind == "container"
            else document.get("Labels")
        ) or {}
        if labels.get(MANAGED_LABEL) != "true":
            return None
        return RuntimeResource(
            kind=kind,
            resource_id=resource_id,
            name=str(document.get("Name") or "").lstrip("/"),
            capsule_id=labels.get(CAPSULE_ID_LABEL),
            episode_id=labels.get(EPISODE_ID_LABEL),
            role=labels.get(RESOURCE_ROLE_LABEL),
        )

    @staticmethod
    def _ownership_labels(capsule_id: str, episode_id: str) -> dict[str, str]:
        return {
            MANAGED_LABEL: "true",
            CAPSULE_ID_LABEL: capsule_id,
            EPISODE_ID_LABEL: episode_id,
        }

    @staticmethod
    def _label_arguments(labels: dict[str, str]) -> list[str]:
        arguments: list[str] = []
        for name, value in labels.items():
            arguments.extend(["--label", f"{name}={value}"])
        return arguments

    @staticmethod
    def _labels_match(
        labels: dict[str, str],
        capsule_id: str,
        episode_id: str,
        role: str,
    ) -> bool:
        return (
            labels.get(MANAGED_LABEL) == "true"
            and labels.get(CAPSULE_ID_LABEL) == capsule_id
            and labels.get(EPISODE_ID_LABEL) == episode_id
            and labels.get(RESOURCE_ROLE_LABEL) == role
        )

    @staticmethod
    def _is_missing_resource_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "no such container" in message or "no such network" in message

    async def _wait_blue_ready(self, handle: CapsuleHandle) -> None:
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            try:
                response = await self.gateway_request(handle, "GET", "/healthz")
                if response.is_success:
                    return
            except Exception:
                pass
            if asyncio.get_running_loop().time() >= deadline:
                raise CapsuleError("Blue gateway readiness timed out")
            await asyncio.sleep(0.1)

    async def gateway_request(
        self,
        handle: CapsuleHandle,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> httpx.Response:
        """Execute a trusted request inside Blue; the Docker socket never reaches the worker."""
        if not handle.blue_container_id:
            raise CapsuleError("capsule has no Blue container")
        if not path.startswith("/") or path.startswith("//"):
            raise CapsuleError("invalid Blue gateway path")
        query = urlencode(params or {}, doseq=True)
        request_path = f"{path}?{query}" if query else path
        safe_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() not in {"authorization", "x-blue-supervisor", "host", "connection"}
        }
        safe_headers["X-Blue-Supervisor"] = handle.supervisor_capability or ""
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "method": method.upper(),
                    "path": request_path,
                    "headers": safe_headers,
                    "json": json_body,
                    "timeout": handle.target_timeout_seconds
                    if path.endswith(("/invoke", "/reset"))
                    else 30,
                },
                separators=(",", ":"),
            ).encode()
        ).decode()
        raw = await self.runner.run(
            "exec",
            handle.blue_container_id,
            "python",
            "-c",
            _BLUE_EXEC_PROXY,
            payload,
            timeout=handle.target_timeout_seconds + 10
            if path.endswith(("/invoke", "/reset"))
            else 60,
        )
        try:
            envelope = json.loads(raw)
            content = base64.b64decode(envelope["body"])
            status_code = int(envelope["status"])
            response_headers = dict(envelope.get("headers") or {})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CapsuleError("invalid response from Blue gateway") from exc
        request = httpx.Request(method, f"http://blue:8080{request_path}")
        return httpx.Response(
            status_code,
            headers=response_headers,
            content=content,
            request=request,
        )


class FirecrackerCapsuleRuntime:
    """HTTP client for an audited Firecracker supervisor; absent configuration fails closed."""

    def __init__(
        self,
        runner_endpoint: str | None = None,
        *,
        supervisor_token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.runner_endpoint = runner_endpoint and runner_endpoint.rstrip("/")
        self.supervisor_token = supervisor_token
        self.client = client

    def _configuration(self) -> tuple[str, dict[str, str]]:
        if not self.runner_endpoint or not self.supervisor_token:
            raise CapsuleError("Firecracker runner is not configured; refusing to weaken isolation")
        return self.runner_endpoint, {"Authorization": f"Bearer {self.supervisor_token}"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        endpoint, headers = self._configuration()
        if self.client is not None:
            response = await self.client.request(
                method, f"{endpoint}{path}", headers=headers, **kwargs
            )
        else:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                response = await client.request(
                    method, f"{endpoint}{path}", headers=headers, **kwargs
                )
        if not response.is_success:
            raise CapsuleError(f"Firecracker supervisor returned {response.status_code}")
        return response

    async def create(self, spec: CapsuleSpec) -> CapsuleHandle:
        result = containment_preflight(spec)
        if not result.verified:
            raise CapsuleError("containment preflight rejected: " + "; ".join(result.violations))
        response = await self._request("POST", "/v1/capsules", json=spec.model_dump(mode="json"))
        return CapsuleHandle.model_validate(response.json())

    async def healthcheck(self, handle: CapsuleHandle) -> bool:
        try:
            response = await self._request("GET", f"/v1/capsules/{handle.capsule_id}/ready")
            return bool(response.json().get("ready"))
        except (CapsuleError, ValueError):
            return False

    async def reset(self, handle: CapsuleHandle) -> None:
        await self._request("POST", f"/v1/capsules/{handle.capsule_id}/reset")

    async def destroy(self, handle: CapsuleHandle) -> None:
        await self._request("DELETE", f"/v1/capsules/{handle.capsule_id}")
