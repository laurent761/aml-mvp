"""Local operator commands; no provider credentials or private bundles in public APIs."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from importlib.resources import files
from pathlib import Path

from .inference_contracts import TargetInferenceProfile
from .scenarios import ScenarioCatalog, TargetBundle, content_hash
from .settings import get_settings
from .storage import Database, Repository


def reference_bundle(
    image: str, inference_profile: TargetInferenceProfile | None = None
) -> TargetBundle:
    document = json.loads(
        files("adversarial_agent_mvp")
        .joinpath("scenario_assets/finance_reference.json")
        .read_text()
    )
    document["manifest"]["image"] = image
    if inference_profile:
        document["execution_mode"] = "model"
        document["manifest"]["inference_profile"] = inference_profile.model_dump(mode="json")
        document["manifest"]["entrypoint"] = [
            "python",
            "-m",
            "aml_reference_target",
            "--mode",
            "model",
        ]
    return TargetBundle.model_validate(document)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate, pin and register operator target bundles"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser(
        "reference", help="Render a reference bundle using a real immutable image reference"
    )
    render.add_argument("--image", required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--inference-profile", type=Path)
    profile = commands.add_parser(
        "inference-profile", help="Export the configured credential-free target model profile"
    )
    profile.add_argument("--output", type=Path, required=True)
    build = commands.add_parser(
        "build-reference", help="Build locally and pin Docker's actual image ID"
    )
    build.add_argument("--context", type=Path, default=Path.cwd())
    build.add_argument("--tag", default="aml-reference-target:local")
    build.add_argument(
        "--cached-runtime-image", help="Offline fallback using a locally cached backend runtime"
    )
    build.add_argument("--output", type=Path, required=True)
    for name in ("validate", "register", "smoke"):
        command = commands.add_parser(name)
        command.add_argument("bundle", type=Path)
        if name == "register":
            command.add_argument("--database-url")
        if name == "smoke":
            command.add_argument("--docker", action="store_true")
            command.add_argument("--blue-image", default="blue-gateway:local")
            command.add_argument(
                "--model-url", help="Trusted local validation only; not the INF-02 capsule broker"
            )
            command.add_argument("--model", help="Operator-selected model; no default")
    args = parser.parse_args()
    try:
        if args.command == "inference-profile":
            from .inference import profile_from_settings

            inference_profile = profile_from_settings(get_settings())
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(inference_profile.model_dump_json(indent=2) + "\n")
            print(json.dumps({"profile": str(args.output), "sha256": inference_profile.sha256}))
            return
        if args.command in {"reference", "build-reference"}:
            if args.command == "build-reference":
                dockerfile = "Dockerfile.cached" if args.cached_runtime_image else "Dockerfile"
                build_args = (
                    ["--build-arg", f"CACHED_RUNTIME_IMAGE={args.cached_runtime_image}"]
                    if args.cached_runtime_image
                    else []
                )
                subprocess.run(
                    [
                        "docker",
                        "build",
                        "-f",
                        str(args.context / "targets/reference" / dockerfile),
                        *build_args,
                        "-t",
                        args.tag,
                        str(args.context),
                    ],
                    check=True,
                )
                image = subprocess.check_output(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", args.tag], text=True
                ).strip()
            else:
                image = args.image
            profile_path = getattr(args, "inference_profile", None)
            selected_profile = (
                TargetInferenceProfile.model_validate_json(profile_path.read_text())
                if profile_path
                else None
            )
            bundle = reference_bundle(image, selected_profile)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(bundle.model_dump_json(indent=2) + "\n")
            print(json.dumps({"bundle": str(args.output), "image": bundle.manifest.image}))
            return
        bundle = TargetBundle.model_validate_json(args.bundle.read_text())
        if args.command == "validate":
            print(
                json.dumps(
                    {
                        "valid": True,
                        "bundle_sha256": content_hash(bundle.model_dump(mode="json")),
                        "mode": bundle.execution_mode,
                    }
                )
            )
        elif args.command == "register":
            database = Database(args.database_url or get_settings().database_url)
            try:
                print(ScenarioCatalog(Repository(database)).register(bundle).model_dump_json())
            finally:
                database.engine.dispose()
        else:
            from .bundle_smoke import smoke_bundle

            print(
                json.dumps(
                    asyncio.run(
                        smoke_bundle(
                            bundle,
                            docker=args.docker,
                            blue_image=args.blue_image,
                            model_url=args.model_url,
                            model_name=args.model,
                        )
                    ),
                    indent=2,
                )
            )
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"bundle operation failed: {exc}\n")


if __name__ == "__main__":
    main()
