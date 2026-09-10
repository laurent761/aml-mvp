from dataclasses import FrozenInstanceError, replace

import pytest

from adversarial_agent_mvp.evaluation import (
    AblationVariant,
    BenchmarkSuiteReference,
    BenchmarkTargetReference,
    EvaluationConfig,
    EvaluationSample,
    EvaluationSplit,
    FailureCategory,
    MlflowEvaluationHook,
    PromotionPolicy,
    aggregate_metrics,
    bootstrap_confidence_interval,
    compare_paired,
    evaluate_promotion,
    run_evaluation,
)


def sample(pair_id: str, **changes) -> EvaluationSample:
    values = {
        "pair_id": pair_id,
        "target_version_id": f"version-{pair_id}",
        "target_variant_id": f"target-{pair_id}",
        "held_out": True,
        "success": True,
        "forbidden_state_ids": ("payment",),
        "steps_to_success": 2,
        "episodes_to_success": 1,
        "total_cost": 5.0,
        "verified_findings": 1,
        "reproduction_attempts": 1,
        "reproductions": 1,
        "attack_fingerprints": (f"attack-{pair_id}",),
    }
    values.update(changes)
    return EvaluationSample(**values)


def config() -> EvaluationConfig:
    return EvaluationConfig(
        evaluation_id="eval-1",
        red_version="red-v1",
        benchmark_version="finance-v1",
        dataset_version="split-v1",
        ablation=AblationVariant.MODEL_FULL_MUTATION,
        bootstrap_iterations=100,
    )


def test_evaluation_dtos_are_frozen_and_config_has_stable_fingerprint():
    current = config()
    assert current.fingerprint == config().fingerprint
    assert current.fingerprint == replace(current, evaluation_id="eval-rerun").fingerprint
    frozen_attribute = "red_version"
    with pytest.raises(FrozenInstanceError):
        setattr(current, frozen_attribute, "changed")
    with pytest.raises(TypeError, match="tuple"):
        sample("bad", forbidden_state_ids=["mutable"])


def test_benchmark_suite_only_references_external_target_versions_and_splits():
    suite = BenchmarkSuiteReference(
        suite_id="finance",
        version="finance-v1",
        targets=(
            BenchmarkTargetReference("target-train", "variant-a", EvaluationSplit.TRAIN),
            BenchmarkTargetReference("target-dev", "variant-e", EvaluationSplit.DEVELOPMENT),
            BenchmarkTargetReference("target-test", "variant-f", EvaluationSplit.TEST),
        ),
    )
    current = EvaluationConfig(
        evaluation_id="eval-suite",
        red_version="red-v1",
        benchmark_version="finance-v1",
        dataset_version="split-v1",
        ablation=AblationVariant.MODEL_FULL_MUTATION,
        benchmark_suite=suite,
    )
    assert current.benchmark_suite is not None
    assert current.benchmark_suite.targets[-1].split == EvaluationSplit.TEST


def frozen_config() -> EvaluationConfig:
    suite = BenchmarkSuiteReference(
        suite_id="finance",
        version="finance-v1",
        targets=(
            BenchmarkTargetReference("target-train", "variant-a", EvaluationSplit.TRAIN),
            BenchmarkTargetReference("target-dev", "variant-e", EvaluationSplit.DEVELOPMENT),
            BenchmarkTargetReference("target-test", "variant-f", EvaluationSplit.TEST),
        ),
    )
    return replace(config(), benchmark_suite=suite)


def suite_sample(pair_id: str, **changes) -> EvaluationSample:
    values = {
        "target_version_id": "target-test",
        "target_variant_id": "variant-f",
        "split": EvaluationSplit.TEST,
        "held_out": True,
    }
    values.update(changes)
    return sample(pair_id, **values)


def test_run_evaluation_rejects_off_suite_and_split_mismatched_samples():
    current = frozen_config()
    off_suite = sample(
        "off-suite",
        target_version_id="unknown-target",
        target_variant_id="unknown-variant",
        split=EvaluationSplit.TRAIN,
        held_out=False,
    )
    with pytest.raises(ValueError, match="not in benchmark suite"):
        run_evaluation(current, [off_suite])

    wrong_split = sample(
        "wrong-split",
        target_version_id="target-test",
        target_variant_id="variant-f",
        split=EvaluationSplit.DEVELOPMENT,
        held_out=False,
    )
    with pytest.raises(ValueError, match="split does not match"):
        run_evaluation(current, [wrong_split])


def test_promotion_requires_frozen_suite_and_paired_baseline():
    candidate = sample("a")
    baseline = sample("a")
    policy = PromotionPolicy()

    with pytest.raises(ValueError, match="frozen benchmark suite"):
        run_evaluation(
            config(),
            [candidate],
            baseline_samples=[baseline],
            promotion_policy=policy,
        )

    with pytest.raises(ValueError, match="paired baseline"):
        run_evaluation(
            frozen_config(),
            [suite_sample("a")],
            promotion_policy=policy,
        )


def test_run_evaluation_defaults_reproducibility_check_to_fail_closed():
    report = run_evaluation(
        frozen_config(),
        [suite_sample("a")],
        baseline_samples=[suite_sample("a")],
        promotion_policy=PromotionPolicy(),
    )

    assert report.promotion is not None
    assert not report.promotion.promoted
    assert report.promotion.reasons == ("config_reproducible",)


def test_aggregate_metrics_covers_plan_metrics():
    samples = [
        sample(
            "a",
            forbidden_state_ids=("payment", "email"),
            total_cost=8.0,
            verified_findings=2,
            reproduction_attempts=3,
            reproductions=2,
            attack_fingerprints=("x", "x"),
            bypass_attempts=2,
            bypasses=1,
            benign_trials=8,
            benign_false_blocks=1,
        ),
        sample(
            "b",
            held_out=False,
            success=False,
            forbidden_state_ids=(),
            steps_to_success=None,
            episodes_to_success=None,
            total_cost=4.0,
            verified_findings=0,
            reproduction_attempts=1,
            reproductions=1,
            attack_fingerprints=("y",),
            benign_trials=2,
            containment_violations=1,
            split=EvaluationSplit.DEVELOPMENT,
            failure_categories=(FailureCategory.NO_PROGRESS,),
        ),
    ]
    metrics = aggregate_metrics(samples)
    assert metrics.attack_success_rate == 0.5
    assert metrics.unique_forbidden_states_reached == 2
    assert metrics.held_out_target_success_rate == 1.0
    assert metrics.median_steps_to_success == 2
    assert metrics.median_episodes_to_success == 1
    assert metrics.cost_per_verified_finding == 6.0
    assert metrics.reproduction_rate == 0.75
    assert metrics.attack_diversity == pytest.approx(2 / 3)
    assert metrics.blue_bypass_rate == 0.5
    assert metrics.benign_false_block_rate == 0.1
    assert metrics.containment_violations == 1
    assert metrics.failure_taxonomy == (("no_progress", 1),)


def test_bootstrap_confidence_interval_is_deterministic():
    first = bootstrap_confidence_interval([0.0, 1.0, 1.0], iterations=200, seed=9)
    second = bootstrap_confidence_interval([0.0, 1.0, 1.0], iterations=200, seed=9)
    assert first == second
    assert first.lower <= first.estimate <= first.upper


def test_paired_comparison_and_promotion_gate_pass_for_non_regression():
    baseline = [
        sample("a", success=False, steps_to_success=None, episodes_to_success=None, total_cost=10),
        sample("b", total_cost=10),
    ]
    candidate = [sample("a"), sample("b")]
    comparison = compare_paired(candidate, baseline, iterations=200, seed=4)
    assert comparison.attack_success_rate_delta.estimate == 0.5
    assert comparison.held_out_asr_delta is not None
    assert comparison.cost_per_verified_finding_delta is not None
    assert comparison.cost_per_verified_finding_delta.estimate == -5.0

    decision = evaluate_promotion(
        comparison,
        PromotionPolicy(minimum_reproduction_rate=1.0),
        config_reproducible=True,
    )
    assert decision.promoted
    assert decision.reasons == ()


def test_promotion_fails_closed_on_missing_or_failed_required_evidence():
    baseline = [sample("a")]
    candidate = [
        sample(
            "a",
            reproduction_attempts=1,
            reproductions=0,
            containment_violations=1,
        )
    ]
    comparison = compare_paired(candidate, baseline, iterations=20)
    decision = evaluate_promotion(
        comparison,
        PromotionPolicy(minimum_reproduction_rate=0.8),
        config_reproducible=False,
    )
    assert not decision.promoted
    assert "reproduction_rate" in decision.reasons
    assert "zero_containment_violations" in decision.reasons
    assert "config_reproducible" in decision.reasons


def test_pairing_rejects_nonmatching_trials():
    with pytest.raises(ValueError, match="pair_id sets"):
        compare_paired([sample("a")], [sample("b")])


def test_run_evaluation_calls_optional_tracking_hook():
    class Hook:
        def __init__(self):
            self.report = None

        def record_evaluation(self, report):
            self.report = report

    hook = Hook()
    report = run_evaluation(config(), [sample("a")], hook=hook)
    assert hook.report is report
    assert report.config_fingerprint == report.config.fingerprint


def test_mlflow_hook_logs_safe_lineage_and_metrics():
    class Tracker:
        def __init__(self):
            self.created = None
            self.logged = None
            self.terminated = None

        def create_run(self, experiment_id, run_name, tags=None):
            self.created = (experiment_id, run_name, tags)
            return "run-1"

        def log_batch(self, run_id, *, metrics=None, params=None):
            self.logged = (run_id, metrics, params)

        def terminate_run(self, run_id, status="FINISHED"):
            self.terminated = (run_id, status)

    tracker = Tracker()
    hook = MlflowEvaluationHook(tracker, "experiment-1")
    report = run_evaluation(config(), [sample("a")], hook=hook)

    assert tracker.created is not None
    assert tracker.created[2] is not None
    assert tracker.logged is not None
    assert tracker.logged[1] is not None
    assert tracker.logged[2] is not None
    assert tracker.created[0] == "experiment-1"
    assert tracker.created[2]["config_fingerprint"] == report.config_fingerprint
    assert tracker.logged[0] == "run-1"
    assert tracker.logged[1]["attack_success_rate"] == 1.0
    assert tracker.logged[2]["dataset_version"] == "split-v1"
    assert tracker.terminated == ("run-1", "FINISHED")
