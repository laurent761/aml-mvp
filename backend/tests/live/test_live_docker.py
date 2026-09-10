from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from adversarial_agent_mvp.capsule import DockerCapsuleRuntime
from adversarial_agent_mvp.contracts import CapsuleSpec

pytestmark = [pytest.mark.live_docker, pytest.mark.containment]


def _docker_prerequisites() -> tuple[str, str, list[str]]:
    if shutil.which("docker") is None:
        pytest.skip("live Docker gate deferred: Docker CLI is not installed")
    probe = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("live Docker gate deferred: Docker daemon is unavailable")
    target_image = os.getenv("MVP_LIVE_TARGET_IMAGE")
    blue_image = os.getenv("MVP_LIVE_BLUE_IMAGE")
    if not target_image or not blue_image:
        pytest.skip(
            "live Docker gate deferred: set MVP_LIVE_TARGET_IMAGE and "
            "MVP_LIVE_BLUE_IMAGE to externally supplied images"
        )
    entrypoint = json.loads(os.getenv("MVP_LIVE_TARGET_ENTRYPOINT_JSON", '["sleep", "30"]'))
    return target_image, blue_image, entrypoint


@pytest.mark.asyncio
async def test_live_capsule_launch_has_one_internal_network_and_cleans_up():
    target_image, blue_image, entrypoint = _docker_prerequisites()
    runtime = DockerCapsuleRuntime(blue_image=blue_image)
    handle = None
    try:
        handle = await runtime.create(
            CapsuleSpec(
                episode_id="live-docker-acceptance",
                image=target_image,
                entrypoint=entrypoint,
                allowed_destination_aliases=["blue"],
            )
        )
        # A running container is not a ready target until trusted scenario bootstrap.
        assert not await runtime.healthcheck(handle)
        assert (await runtime.gateway_request(handle, "GET", "/healthz")).is_success
        proof = await runtime.verify_network_boundary(handle)
        assert proof.verified
        assert proof.network_internal
        inventory = await runtime.inventory()
        owned = [item for item in inventory if item.capsule_id == handle.capsule_id]
        assert {item.role for item in owned} == {"blue", "target", "network"}
        reconciliation = await runtime.reconcile_orphans({handle.capsule_id})
        assert reconciliation.orphaned == 0
    finally:
        if handle is not None:
            await runtime.destroy(handle)
