import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from adversarial_agent_mvp.api import create_app
from adversarial_agent_mvp.capsule import CapsuleError, DockerCapsuleRuntime
from adversarial_agent_mvp.image_readiness import (
    ImageReadiness,
    ImageUnavailable,
    inspect_image,
    unavailable,
)
from adversarial_agent_mvp.orchestrator import CampaignRunner
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.supervisor import CapsuleSupervisorClient, create_supervisor_app
from tests.helpers import MemoryEnvironment, SequenceModel
from tests.integration.test_durable_recovery import seed

DIGEST = "a" * 64
IMAGE = "localhost:5001/aml/test@sha256:" + DIGEST
IMAGE_ID = "sha256:" + "b" * 64


class ImageRunner:
    def __init__(
        self, *, missing=False, pull_fails=False, wrong_identity=False, architecture="arm64"
    ):
        self.missing = missing
        self.pull_fails = pull_fails
        self.wrong_identity = wrong_identity
        self.architecture = architecture
        self.calls = []

    async def run(self, *args, timeout=30):
        self.calls.append(args)
        if args[0] == "info":
            return "linux/aarch64"
        if args[0] == "pull":
            if self.pull_fails:
                raise CapsuleError("registry unavailable with private credentials")
            self.missing = False
            return "pulled"
        if args[:2] == ("image", "inspect"):
            if self.missing:
                raise CapsuleError("No such image: " + args[-1])
            return json.dumps(
                [
                    {
                        "Id": IMAGE_ID,
                        "RepoDigests": [] if self.wrong_identity else [IMAGE],
                        "Os": "linux",
                        "Architecture": self.architecture,
                    }
                ]
            )
        raise AssertionError(f"Unexpected Docker side effect: {args}")


@pytest.mark.asyncio
async def test_missing_image_restored_by_exact_digest_before_any_containers():
    runner = ImageRunner(missing=True)
    result = await DockerCapsuleRuntime(runner).prepare_image(IMAGE)
    assert result.status == "READY" and result.restored
    assert result.image_id == IMAGE_ID and result.image == IMAGE
    assert [call for call in runner.calls if call[0] == "pull"] == [("pull", IMAGE)]


@pytest.mark.asyncio
async def test_read_only_check_does_not_pull():
    runner = ImageRunner(missing=True)
    result = await inspect_image(runner, IMAGE)
    assert result.code == "TARGET_IMAGE_MISSING"
    assert not any(call[0] == "pull" for call in runner.calls)


@pytest.mark.asyncio
async def test_local_id_is_never_treated_as_registry_digest():
    runner = ImageRunner(missing=True)
    result = await inspect_image(runner, IMAGE_ID, restore=True)
    assert result.status == "UNAVAILABLE" and not result.recoverable
    assert "legacy" in result.technical_detail.lower()
    assert not any(call[0] == "pull" for call in runner.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"pull_fails": True, "missing": True}, "IMAGE_RESTORE_FAILED"),
        ({"wrong_identity": True}, "IMAGE_IDENTITY_MISMATCH"),
        ({"architecture": "amd64"}, "IMAGE_PLATFORM_MISMATCH"),
    ],
)
async def test_unavailable_cases_fail_closed(kwargs, code):
    with pytest.raises(ImageUnavailable) as error:
        await DockerCapsuleRuntime(ImageRunner(**kwargs)).prepare_image(IMAGE)
    assert error.value.readiness.code == code
    assert "credentials" not in error.value.readiness.model_dump_json()


@pytest.mark.asyncio
async def test_mutable_tags_are_never_resolved_to_a_new_version():
    runner = ImageRunner()
    result = await inspect_image(runner, "localhost:5001/aml/test:latest", restore=True)
    assert result.code == "IMAGE_NOT_PINNED" and not runner.calls


@pytest.mark.asyncio
async def test_daemon_error_is_not_misreported_as_missing_image():
    class Disconnected(ImageRunner):
        async def run(self, *args, timeout=30):
            raise CapsuleError("Docker connection failed")

    result = await inspect_image(Disconnected(), IMAGE, restore=True)
    assert result.code == "IMAGE_CHECK_FAILED"


@pytest.mark.asyncio
async def test_blocked_campaign_creates_zero_experiments_and_does_not_retry(
    repository, manifest, task
):
    campaign, _ = seed(repository, manifest, task)

    class MissingLifecycle:
        checks = 0

        async def prepare(self, manifest):
            self.checks += 1
            raise ImageUnavailable(
                unavailable(
                    manifest.image,
                    "TARGET_IMAGE_MISSING",
                    "The saved image for this target version is unavailable.",
                )
            )

        async def provision(self, *args):
            raise AssertionError("must not provision an experiment")

    runtime = MissingLifecycle()
    runner = CampaignRunner(repository, SequenceModel([]), lambda *_: MemoryEnvironment(), runtime)
    await runner.run(campaign.id)
    await runner.run(campaign.id)
    assert runtime.checks == 1
    stored = repository.get_campaign(campaign.id)
    assert stored.status == "BLOCKED" and stored.started_at is None and stored.episodes_started == 0
    assert repository.list_episodes(campaign.id) == []
    assert repository.get_image_readiness(campaign.id)["code"] == "TARGET_IMAGE_MISSING"
    with TestClient(create_app(database=repository.db, settings=Settings())) as client:
        response = client.get(f"/v1/campaigns/{campaign.id}")
        assert response.status_code == 200
        assert response.json()["image_readiness"]["status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_image_disappears_after_preflight_blocks_once(repository, manifest, task):
    campaign, _ = seed(repository, manifest, task)

    class RaceLifecycle:
        provisions = 0

        async def prepare(self, manifest):
            return ImageReadiness(
                status="READY",
                code="IMAGE_READY",
                image=manifest.image,
                message="Ready",
                recoverable=True,
                checked_at=datetime.now(UTC),
            )

        async def provision(self, *args):
            self.provisions += 1
            raise ImageUnavailable(
                unavailable(manifest.image, "TARGET_IMAGE_MISSING", "Target image unavailable")
            )

        async def destroy(self, handle):
            pass

    runtime = RaceLifecycle()
    runner = CampaignRunner(repository, SequenceModel([]), lambda *_: MemoryEnvironment(), runtime)
    await runner.run(campaign.id)
    await runner.run(campaign.id)
    assert runtime.provisions == 1
    assert repository.get_campaign(campaign.id).status == "BLOCKED"
    episodes = repository.list_episodes(campaign.id)
    assert len(episodes) == 1 and episodes[0].error
    assert not repository.get_episode(episodes[0].id)[1]


@pytest.mark.asyncio
async def test_supervisor_auth_and_structured_error_round_trip():
    settings = Settings(capsule_supervisor_token="s" * 32)
    app = create_supervisor_app(
        settings, DockerCapsuleRuntime(ImageRunner(missing=True, pull_fails=True))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://supervisor"
    ) as http:
        assert (await http.post("/v1/images/check", json={"image": IMAGE})).status_code == 401
        client = CapsuleSupervisorClient(
            "http://supervisor", settings.capsule_supervisor_token, http
        )
        with pytest.raises(ImageUnavailable) as error:
            await client.prepare_image(IMAGE)
        assert error.value.readiness.code == "IMAGE_RESTORE_FAILED"


@pytest.mark.asyncio
async def test_cancelled_check_propagates_cancellation():
    class Cancelled(ImageRunner):
        async def run(self, *args, timeout=30):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await inspect_image(Cancelled(), IMAGE, restore=True)
