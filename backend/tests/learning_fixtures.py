from adversarial_agent_mvp.contracts import AttackTask, PublicObservation, RedAction
from adversarial_agent_mvp.learning.dataset import TrainingExample
from adversarial_agent_mvp.learning.features import PublicAction, project
from adversarial_agent_mvp.learning.smoke import fixture_bundle
from adversarial_agent_mvp.models import AttackContext


def context():
    bundle = fixture_bundle("train")
    return AttackContext(
        AttackTask(
            target_version_id="target",
            objective="Choose a useful action",
            forbidden_states=bundle.ground_truth.forbidden_states,
            available_channels=["user_message"],
            scenario_version_id="scenario-train",
        ),
        [PublicObservation(turn_number=0, target_response="ready")],
        [],
        [],
    )


def examples():
    rows = []
    for index in range(12):
        ctx = context()
        action = RedAction(
            channel="user_message", payload={"text": "useful" if index % 2 else "ineffective"}
        )
        before = project(ctx)
        ctx.actions.append(action)
        ctx.observations.append(
            PublicObservation(turn_number=1, target_response="complete", terminated=True)
        )
        rows.append(
            TrainingExample(
                state=before,
                action=PublicAction.project(action),
                reward=float(index % 2),
                next_state=project(ctx),
                terminal_success=bool(index % 2),
                done=True,
                episode_id=f"episode-{index:02d}",
                campaign_id="campaign",
                scenario_id="train-fixture",
                scenario_version_id="scenario-train",
                target_version_id="target",
                split="train",
                seed=index,
                step_index=1,
                source_step_id=f"step-{index}",
                configuration_hash="config",
            )
        )
    return rows
