"""Verify immutable target images on the execution daemon, restoring only exact digests."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel

_LOCAL_ID = re.compile(r"sha256:[a-f0-9]{64}")
_REGISTRY_REF = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}")


class ImageReadiness(BaseModel):
    status: Literal["READY", "UNAVAILABLE"]
    code: str
    message: str
    image: str
    image_id: str | None = None
    recoverable: bool = False
    restored: bool = False
    technical_detail: str | None = None
    checked_at: datetime


class ImageUnavailable(RuntimeError):
    def __init__(self, readiness: ImageReadiness):
        self.readiness = readiness
        super().__init__(readiness.message)


class ImageCommandRunner(Protocol):
    async def run(self, *arguments: str, timeout: float = 30) -> str: ...


def unavailable(image: str, code: str, message: str, detail: str | None = None) -> ImageReadiness:
    return ImageReadiness(
        status="UNAVAILABLE",
        code=code,
        message=message,
        image=image,
        recoverable=bool(_REGISTRY_REF.fullmatch(image)),
        technical_detail=detail,
        checked_at=datetime.now(UTC),
    )


def _architecture(value: str) -> str:
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(value, value)


async def inspect_image(
    runner: ImageCommandRunner, image: str, *, restore: bool = False
) -> ImageReadiness:
    recoverable = bool(_REGISTRY_REF.fullmatch(image))
    if not recoverable and not _LOCAL_ID.fullmatch(image):
        return unavailable(
            image,
            "IMAGE_NOT_PINNED",
            "This target needs an immutable image version before it can run.",
        )
    restored = False
    try:
        # Check the daemon before interpreting a failed inspect as a missing image.
        platform = (
            await runner.run("info", "--format", "{{.OSType}}/{{.Architecture}}", timeout=10)
        ).strip()
        try:
            raw = await runner.run("image", "inspect", image, timeout=10)
        except Exception as exc:
            if "no such image" not in str(exc).lower() and "no such object" not in str(exc).lower():
                raise
            if not restore or not recoverable:
                return unavailable(
                    image,
                    "TARGET_IMAGE_MISSING",
                    "The saved image for this target version is unavailable.",
                    "Restore the exact image from its registry."
                    if recoverable
                    else "This legacy version records only a local Docker image ID. Restore the original image from a backup or select a newly prepared target version.",
                )
            try:
                await runner.run("pull", image, timeout=90)
            except Exception:
                return unavailable(
                    image,
                    "IMAGE_RESTORE_FAILED",
                    "AML could not restore this target version.",
                    "The exact image could not be pulled. Check the registry availability, access and image retention, then try Restore this version again.",
                )
            raw = await runner.run("image", "inspect", image, timeout=10)
            restored = True
        document: dict[str, Any] = json.loads(raw)[0]
        image_id = document.get("Id", "")
        if (not recoverable and image_id != image) or (
            recoverable and image not in (document.get("RepoDigests") or [])
        ):
            return unavailable(
                image,
                "IMAGE_IDENTITY_MISMATCH",
                "The available image does not match the saved target version.",
            )
        os_name, architecture = platform.split("/", 1)
        if document.get("Os") != os_name or _architecture(
            str(document.get("Architecture"))
        ) != _architecture(architecture):
            return unavailable(
                image,
                "IMAGE_PLATFORM_MISMATCH",
                "This target image was built for a different execution platform.",
                f"Image: {document.get('Os')}/{document.get('Architecture')}; execution daemon: {platform}.",
            )
        return ImageReadiness(
            status="READY",
            code="IMAGE_READY",
            message="Image verified on the execution host. AML will check again when the campaign starts.",
            image=image,
            image_id=image_id,
            recoverable=recoverable,
            restored=restored,
            checked_at=datetime.now(UTC),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return unavailable(
            image,
            "IMAGE_CHECK_FAILED",
            "AML could not verify the target image on the execution host.",
            "Check the capsule supervisor and its Docker connection, then retry.",
        )
