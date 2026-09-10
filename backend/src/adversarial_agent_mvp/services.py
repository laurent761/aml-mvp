from __future__ import annotations

from functools import partial

from .blue import BlueEngine
from .capsule import CapsuleRuntime
from .contracts import CapsuleHandle, CapsuleSpec, PolicyDocument, TargetManifest
from .environment import AgentEnvironment, BlackBoxEnvironment
from .policy import PolicyEngine
from .scenarios import ScenarioCatalog
from .settings import get_settings
from .storage import Repository
from .supervisor import CapsuleSupervisorClient
from .target_adapter import HttpTargetAdapter, destination_routes_for_manifest
from .verifier import DeterministicVerifier
from .virtual_world import VirtualWorld


class DockerLifecycle:
    def __init__(self, runtime: CapsuleRuntime | None = None):
        if runtime is not None:
            self.runtime = runtime
        else:
            settings = get_settings()
            if not settings.capsule_supervisor_url:
                raise RuntimeError(
                    "CAPSULE_SUPERVISOR_URL is required; workers never receive Docker access"
                )
            self.runtime = CapsuleSupervisorClient(
                settings.capsule_supervisor_url,
                settings.capsule_supervisor_token,
            )

    async def provision(self, episode_id: str, manifest: TargetManifest) -> CapsuleHandle:
        routes = destination_routes_for_manifest(manifest)
        spec = CapsuleSpec(
            episode_id=episode_id,
            image=manifest.image,
            entrypoint=manifest.entrypoint,
            environment={},
            resource_limits=manifest.resource_limits,
            allowed_destination_aliases=list(routes),
            environment_aliases=manifest.environment_aliases,
            destination_routes=routes,
            inference_profile=manifest.inference_profile,
        )
        return await self.runtime.create(spec)

    async def healthcheck(self, handle: CapsuleHandle | None) -> bool:
        return bool(handle and await self.runtime.healthcheck(handle))

    async def reset(self, handle: CapsuleHandle | None) -> None:
        if handle:
            await self.runtime.reset(handle)

    async def destroy(self, handle: CapsuleHandle | None) -> None:
        if handle:
            await self.runtime.destroy(handle)


def build_environment(
    episode_id: str,
    manifest: TargetManifest,
    policies: list[PolicyDocument],
    capsule_handle: CapsuleHandle | None,
    *,
    repository: Repository | None = None,
) -> BlackBoxEnvironment:
    world = VirtualWorld()
    blue = BlueEngine(PolicyEngine(policies, capsule_mode=True), world)
    target = HttpTargetAdapter(
        manifest,
        capsule_handle=capsule_handle,
        policies=policies,
        allow_direct=capsule_handle is None,
        scenario_loader=ScenarioCatalog(repository).runtime if repository else None,
        inference_recorder=partial(repository.record_target_inference, episode_id)
        if repository
        else None,
    )
    return AgentEnvironment(episode_id, target, blue, DeterministicVerifier())
