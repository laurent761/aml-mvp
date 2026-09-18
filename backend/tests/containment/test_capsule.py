import asyncio
import base64
import json

import pytest

from adversarial_agent_mvp.capsule import (
    CAPSULE_ID_LABEL,
    EPISODE_ID_LABEL,
    MANAGED_LABEL,
    RESOURCE_ROLE_LABEL,
    CapsuleError,
    DockerCapsuleRuntime,
    FirecrackerCapsuleRuntime,
    containment_preflight,
)
from adversarial_agent_mvp.contracts import CapsuleHandle, CapsuleSpec

pytestmark = pytest.mark.containment


class Runner:
    def __init__(self, fail_on=None, extra_network=False):
        self.calls = []
        self.fail_on = fail_on
        self.extra_network = extra_network
        self.network_name = "capsule"
        self.run_count = 0
        self.labels: dict[str, dict[str, str]] = {}

    @staticmethod
    def _labels(args):
        labels = {}
        for index, value in enumerate(args):
            if value == "--label":
                name, label_value = args[index + 1].split("=", 1)
                labels[name] = label_value
        return labels

    async def run(self, *args, timeout: float = 30):
        self.calls.append(args)
        if args[0] == "info":
            return "linux/arm64"
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": "sha256:" + "b" * 64, "RepoDigests": [args[-1]], "Os": "linux", "Architecture": "arm64"}])
        if self.fail_on and self.fail_on in args:
            raise CapsuleError("failed with secret-value")
        if args[:2] == ("network", "create"):
            self.network_name = args[-1]
            self.labels["network-id"] = self._labels(args)
            return "network-id"
        if args[0] == "run":
            self.run_count += 1
            resource_id = "blue-id" if self.run_count == 1 else "target-id"
            self.labels[resource_id] = self._labels(args)
            return resource_id
        if args[0] == "port":
            return "127.0.0.1:49152"
        if args[0] == "exec":
            return json.dumps(
                {
                    "status": 200,
                    "headers": {"content-type": "application/json"},
                    "body": base64.b64encode(b"{}").decode(),
                }
            )
        if args[0] == "inspect" and "--format" not in args:
            networks = {self.network_name: {}}
            if self.extra_network:
                networks["bridge"] = {}
            return json.dumps(
                [
                    {
                        "Name": args[1],
                        "Config": {"Labels": self.labels[args[1]]},
                        "NetworkSettings": {"Networks": networks},
                    }
                ]
            )
        if args[:2] == ("network", "inspect"):
            return json.dumps(
                [
                    {
                        "Name": self.network_name,
                        "Internal": True,
                        "Labels": self.labels["network-id"],
                        "Containers": {"blue-id": {}, "target-id": {}},
                    }
                ]
            )
        if "--format" in args:
            return "true"
        return ""


def spec(**changes):
    values = {
        "episode_id": "e1",
        "image": ("target@sha256:" + "a" * 64),
        "entrypoint": ["run"],
        "allowed_destination_aliases": ["payments"],
    }
    values.update(changes)
    return CapsuleSpec(**values)


@pytest.mark.parametrize(
    ("change", "violation"),
    [
        ({"privileged": True}, "privileged"),
        ({"host_network": True}, "host network"),
        ({"host_mounts": ["/host:/mnt"]}, "host mounts"),
        ({"docker_socket": True}, "Docker socket"),
        ({"external_dns": True}, "external DNS"),
        ({"environment": {"API_TOKEN": "real"}}, "secret-like"),
        ({"allowed_destination_aliases": ["http://production"]}, "opaque"),
    ],
)
def test_preflight_fails_closed(change, violation):
    result = containment_preflight(spec(**change))
    assert not result.verified
    assert violation in " ".join(result.violations)


@pytest.mark.asyncio
async def test_docker_runtime_uses_internal_network_and_hardening_flags():
    runner = Runner()
    runtime = DockerCapsuleRuntime(runner)
    handle = await runtime.create(spec())
    assert handle.network_id == "network-id"
    flat = [item for call in runner.calls for item in call]
    assert "--internal" in flat
    assert "--read-only" in flat
    assert "no-new-privileges" in flat
    assert "--cap-drop" in flat
    assert handle.blue_container_id == "blue-id"
    assert handle.runtime_containment_proof is not None
    assert handle.runtime_containment_proof.verified
    assert handle.runtime_containment_proof.container_network_counts == {
        "blue": 1,
        "target": 1,
    }
    assert handle.runtime_containment_proof.actual_member_count == 2
    for resource_labels in runner.labels.values():
        assert resource_labels[MANAGED_LABEL] == "true"
        assert resource_labels[CAPSULE_ID_LABEL] == handle.capsule_id
        assert resource_labels[EPISODE_ID_LABEL] == "e1"
    await runtime.destroy(handle)
    removed = [call[-1] for call in runner.calls if call[:2] == ("rm", "-f")]
    assert {"blue-id", "target-id"}.issubset(removed)


@pytest.mark.asyncio
async def test_manifest_entrypoint_is_forced_instead_of_passed_as_image_cmd():
    runner = Runner()
    runtime = DockerCapsuleRuntime(runner)

    await runtime.create(spec(entrypoint=["python", "-m", "agent"]))

    target_run = [call for call in runner.calls if call[0] == "run"][1]
    image_index = target_run.index("target@sha256:" + "a" * 64)
    assert target_run[image_index - 2 : image_index] == ("--entrypoint", "python")
    assert target_run[image_index + 1 :] == ("-m", "agent")


@pytest.mark.asyncio
async def test_unexpected_network_attachment_triggers_teardown():
    runner = Runner(extra_network=True)
    with pytest.raises(CapsuleError, match="unexpected network"):
        await DockerCapsuleRuntime(runner).create(spec())
    assert any(call[:2] == ("rm", "-f") for call in runner.calls)


@pytest.mark.asyncio
async def test_startup_error_is_redacted_and_cleanup_runs():
    runner = Runner(fail_on=("target@sha256:" + "a" * 64))
    runtime = DockerCapsuleRuntime(runner)
    bad = spec(environment={"PUBLIC_ALIAS": "secret-value"})
    with pytest.raises(CapsuleError) as raised:
        await runtime.create(bad)
    assert "secret-value" not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


@pytest.mark.asyncio
async def test_firecracker_boundary_refuses_unconfigured_runner():
    with pytest.raises(CapsuleError, match="not configured"):
        await FirecrackerCapsuleRuntime().create(spec())


class InventoryRunner:
    def __init__(self):
        self.containers = {
            "orphan-blue": self._labels("capsule-orphan", "episode-old", "blue"),
            "active-blue": self._labels("capsule-active", "episode-live", "blue"),
        }
        self.networks = {
            "orphan-network": self._labels("capsule-orphan", "episode-old", "network"),
            "active-network": self._labels("capsule-active", "episode-live", "network"),
        }
        self.removed: list[tuple[str, str]] = []

    @staticmethod
    def _labels(capsule_id, episode_id, role):
        return {
            MANAGED_LABEL: "true",
            CAPSULE_ID_LABEL: capsule_id,
            EPISODE_ID_LABEL: episode_id,
            RESOURCE_ROLE_LABEL: role,
        }

    async def run(self, *args, timeout: float = 30):
        if args[:2] == ("ps", "-aq"):
            return "\n".join(self.containers)
        if args[:3] == ("network", "ls", "-q"):
            return "\n".join(self.networks)
        if args[0] == "inspect" and "--format" not in args:
            resource_id = args[1]
            if resource_id not in self.containers:
                raise CapsuleError("No such container")
            return json.dumps(
                [
                    {
                        "Name": f"/{resource_id}",
                        "Config": {"Labels": self.containers[resource_id]},
                    }
                ]
            )
        if args[:2] == ("network", "inspect"):
            resource_id = args[2]
            if resource_id not in self.networks:
                raise CapsuleError("No such network")
            return json.dumps(
                [{"Name": resource_id, "Labels": self.networks[resource_id]}]
            )
        if args[:2] == ("rm", "-f"):
            resource_id = args[2]
            self.containers.pop(resource_id, None)
            self.removed.append(("container", resource_id))
            return ""
        if args[:2] == ("network", "rm"):
            resource_id = args[2]
            self.networks.pop(resource_id, None)
            self.removed.append(("network", resource_id))
            return ""
        return ""


@pytest.mark.asyncio
async def test_inventory_and_orphan_reconciliation_preserve_active_capsules_and_are_idempotent():
    runner = InventoryRunner()
    runtime = DockerCapsuleRuntime(runner)  # type: ignore[arg-type]

    inventory = await runtime.inventory()
    assert len(inventory) == 4
    assert {item.capsule_id for item in inventory} == {"capsule-orphan", "capsule-active"}

    result = await runtime.reconcile_orphans({"capsule-active"})
    assert result.discovered == 4
    assert result.orphaned == 2
    assert result.removed == 2
    assert runner.removed == [
        ("container", "orphan-blue"),
        ("network", "orphan-network"),
    ]

    repeated = await runtime.reconcile_orphans({"capsule-active"})
    assert repeated.discovered == 2
    assert repeated.orphaned == 0
    assert repeated.removed == 0


@pytest.mark.asyncio
async def test_cancellation_during_create_still_removes_partial_resources():
    class BlockingRunner(Runner):
        def __init__(self):
            super().__init__()
            self.target_started = asyncio.Event()

        async def run(self, *args, timeout: float = 30):
            if args and args[0] == "run" and ("target@sha256:" + "a" * 64) in args:
                self.target_started.set()
                await asyncio.Event().wait()
            return await super().run(*args, timeout=timeout)

    runner = BlockingRunner()
    task = asyncio.create_task(DockerCapsuleRuntime(runner).create(spec()))
    await runner.target_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("rm", "-f", "blue-id") in runner.calls
    assert any(call[:2] == ("network", "rm") for call in runner.calls)


@pytest.mark.asyncio
async def test_cancellation_during_gateway_teardown_still_removes_all_resources():
    class BlockingGatewayRuntime(DockerCapsuleRuntime):
        def __init__(self, runner):
            super().__init__(runner)
            self.teardown_started = asyncio.Event()

        async def gateway_request(self, *args, **kwargs):
            self.teardown_started.set()
            await asyncio.Event().wait()

    runner = Runner()
    runtime = BlockingGatewayRuntime(runner)
    handle = CapsuleHandle(
        capsule_id="capsule-cancelled",
        episode_id="episode-cancelled",
        target_container_id="target-id",
        blue_container_id="blue-id",
        network_id="network-id",
        blue_alias="blue",
    )
    task = asyncio.create_task(runtime.destroy(handle))
    await runtime.teardown_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("rm", "-f", "target-id") in runner.calls
    assert ("rm", "-f", "blue-id") in runner.calls
    assert ("network", "rm", "network-id") in runner.calls
