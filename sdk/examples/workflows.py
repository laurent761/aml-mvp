"""python examples/workflows.py <workflow>; no private backend imports."""
import argparse
import asyncio
import os
from pathlib import Path

from aml_research import Client


async def one_episode(client, group_id=None):
    bundle_id = os.environ.get("AML_BUNDLE_ID") or (await client.catalog())[0]["bundle_id"]
    async with client.session(bundle_id=bundle_id, group_id=group_id,
                              run_id=os.environ.get("AML_RUN_ID")) as session:
        initial = await session.reset()
        # Replace this decision with your model(initial.public_observation).
        # Never include Outcome, measurements or baseline_reward in attacker prompts.
        print("Public response:", initial.public_observation.target_response)
        result = await session.step({"channel": "user_message", "payload": {"text": "Process invoice-001."}})
        print(session.id, result.episode_id, result.outcome.model_dump())


async def main(workflow):
    async with Client(os.environ["AML_API_URL"], os.environ["AML_TOKEN"], concurrency=2) as client:
        if workflow == "episode":
            await one_episode(client)
        elif workflow == "concurrent":
            await asyncio.gather(*(one_episode(client, "related-attempts") for _ in range(2)))
        elif workflow == "export":
            snapshot = await client.export_dataset(run_ids=[os.environ["AML_RUN_ID"]], split="development", view="model_input", format="jsonl")
            for _ in range(300):
                detail = await client.dataset(snapshot["id"])
                if detail["status"] == "completed":
                    manifest = detail["manifest"]
                    await client.download(manifest["artifact_id"], "records.jsonl", sha256=manifest["sha256"])
                    print(manifest)
                    return
                if detail["status"] == "failed":
                    raise RuntimeError("dataset export failed")
                await asyncio.sleep(1)
            raise TimeoutError("export pending; recover snapshot " + snapshot["id"])
        elif workflow == "training":
            run = await client.create_run(name="External training", code_revision=os.environ["AML_CODE_REVISION"],
                dataset_ids=[os.environ["AML_DATASET_ID"]], external_run_reference=os.environ.get("AML_EXTERNAL_RUN"),
                resume_checkpoint_id=os.environ.get("AML_RESUME_CHECKPOINT_ID"), configuration={"training_config_reference": os.environ["AML_TRAINING_CONFIG"]})
            await client.report_event(run["id"], status="running", log="External process started")
            print("Your training process reports progress to run:", run["id"])
        elif workflow == "checkpoint":
            run_id = os.environ["AML_RUN_ID"]
            path = Path(os.environ["AML_CHECKPOINT_FILE"])
            uploaded = await client.upload(path, run_id=run_id)
            checkpoint = await client.register_checkpoint(run_id=run_id, name=path.stem, kind="full_model",
                tokenizer_revision=os.environ["AML_TOKENIZER_REVISION"],
                files=[{"path": path.name, "artifact_id": uploaded["artifact_id"]}])
            await client.report_event(run_id, status="completed", artifact_ids=[uploaded["artifact_id"]])
            print(checkpoint)
        elif workflow == "runtime":
            print(await client.register_runtime(name=os.environ["AML_RUNTIME_NAME"], version=os.environ["AML_RUNTIME_VERSION"],
                endpoint=os.environ["AML_RUNTIME_ENDPOINT"], model=os.environ["AML_MODEL_NAME"],
                checkpoint_id=os.environ["AML_CHECKPOINT_ID"], credential_ref=os.environ.get("AML_CREDENTIAL_REF")))
        elif workflow == "evaluation":
            evaluation = await client.submit_evaluation(suite_id=os.environ["AML_SUITE_ID"],
                baseline_checkpoint_id=os.environ["AML_BASELINE_CHECKPOINT_ID"], candidate_checkpoint_id=os.environ["AML_CHECKPOINT_ID"],
                baseline_runtime_id=os.environ["AML_BASELINE_RUNTIME_ID"], candidate_runtime_id=os.environ["AML_RUNTIME_ID"])
            print("Evaluation:", evaluation["id"])
            for _ in range(1800):
                result = await client.evaluation(evaluation["id"])
                if result["status"] in {"completed", "failed"}:
                    print(result)
                    return
                await asyncio.sleep(2)
            raise TimeoutError("evaluation remains active; recover using its ID")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow", choices=["episode", "concurrent", "export", "training", "checkpoint", "runtime", "evaluation"])
    asyncio.run(main(parser.parse_args().workflow))
