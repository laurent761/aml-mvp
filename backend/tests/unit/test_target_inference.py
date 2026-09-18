import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from adversarial_agent_mvp.inference import TargetInferenceBroker, profile_from_settings
from adversarial_agent_mvp.inference_contracts import (
    InferenceMessage,
    InferenceWork,
    TargetInferenceRequest,
    inference_hash,
)
from adversarial_agent_mvp.inference_mailbox import InferenceMailbox
from tests.helpers import IsolatedSettings


def configured(**changes):
    return IsolatedSettings(
        otel_enabled=False,
        **{
            "target_model_provider": "local_openai_compatible",
            "target_model_base_url": "http://operator.test/v1/",
            "target_model_name": "operator-selected-test-fixture",
            "target_model_api_key": "private-provider-key",
            **changes,
        },
    )


def work(broker, request_id="request-1", episode_id="episode-1", **changes):
    request = TargetInferenceRequest(
        messages=[InferenceMessage(role="user", content="Return JSON")], **changes
    )
    return InferenceWork(
        request_id=request_id,
        episode_id=episode_id,
        profile_sha256=broker.profile.sha256,
        request_hash=inference_hash(request.model_dump(mode="json")),
        request=request,
    )


def response(**changes):
    return httpx.Response(
        200,
        headers={"x-request-id": "provider-request-1"},
        json={
            "choices": [{"message": {"content": '{"type":"final","response":"ok"}'}}],
            "model": "resolved-test-revision",
            "system_fingerprint": "fp-123",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            **changes,
        },
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"target_model_provider": "disabled"},
        {"target_model_name": None},
        {"target_model_base_url": "http://user:key@operator.test/v1"},
        {"target_model_base_url": "http://operator.test/v1?key=secret"},
        {"target_model_base_url": "file:///etc/passwd"},
        {"target_model_provider": "hosted_openai_compatible"},
        {
            "target_model_provider": "hosted_openai_compatible",
            "target_model_base_url": "https://operator.test/v1",
            "target_model_api_key": None,
        },
    ],
)
def test_operator_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        profile_from_settings(configured(**changes))


async def test_broker_pins_configuration_scopes_correlates_and_meters_once():
    calls = []

    def provider(request):
        calls.append(request)
        return response()

    broker = TargetInferenceBroker(
        configured(target_model_input_cost_per_million=1, target_model_output_cost_per_million=2),
        transport=httpx.MockTransport(provider),
    )
    try:
        broker.register("episode-1", broker.profile)
        first, second = await asyncio.gather(
            broker.complete(work(broker)), broker.complete(work(broker))
        )
        assert first == second and len(calls) == 1
        assert calls[0].url == "http://operator.test/v1/chat/completions"
        assert calls[0].headers["Authorization"] == "Bearer private-provider-key"
        assert calls[0].headers["X-Request-ID"] == "request-1"
        assert json.loads(calls[0].content)["model"] == broker.profile.model
        assert first.audit.total_tokens == 15 and first.audit.cost == pytest.approx(0.00002)
        assert (
            first.audit.usage_source == "provider" and first.audit.cost_source == "operator_rates"
        )
        assert first.audit.resolved_model == "resolved-test-revision"
        assert first.audit.reproducibility == "provider_revision_unavailable"
        assert "private-provider-key" not in first.model_dump_json()
        with pytest.raises(ValueError, match="different input"):
            await broker.complete(work(broker, model=broker.profile.model))
        with pytest.raises(ValueError, match="not active"):
            await broker.complete(work(broker, episode_id="other"))
        with pytest.raises(ValueError, match="integrity"):
            await broker.complete(work(broker).model_copy(update={"profile_sha256": "wrong"}))
        with pytest.raises(ValueError, match="match"):
            broker.register("other", broker.profile.model_copy(update={"model": "another"}))
        await broker.remove("episode-1")
        with pytest.raises(ValueError, match="not active"):
            await broker.complete(work(broker))
    finally:
        await broker.aclose()


@pytest.mark.parametrize(
    ("settings", "override", "code"),
    [
        ({"target_model_max_requests": 1}, {}, "request_budget"),
        ({"target_model_max_total_tokens": 1}, {}, "token_budget"),
        ({"target_model_max_cost": 0, "target_model_input_cost_per_million": 1}, {}, "cost_budget"),
        ({}, {"model": "unapproved"}, "model_not_allowed"),
    ],
)
async def test_budget_is_reserved_before_dispatch(settings, override, code):
    calls = []
    broker = TargetInferenceBroker(
        configured(**settings),
        transport=httpx.MockTransport(lambda r: calls.append(r) or response()),
    )
    try:
        broker.register("episode-1", broker.profile)
        if code == "request_budget":
            await broker.complete(work(broker, "first"))
        result = await broker.complete(work(broker, **override))
        assert result.audit.status == "REJECTED" and result.audit.error_code == code
        assert result.audit.total_tokens == 0
        assert len(calls) == (1 if code == "request_budget" else 0)
    finally:
        await broker.aclose()


@pytest.mark.parametrize(
    "kind", ["timeout", "slow", "redirect", "error", "oversize", "malformed", "malformed_usage"]
)
async def test_provider_failure_is_bounded_sanitized_and_never_retried(kind):
    calls = []

    async def provider(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("private-provider-key")
        if kind == "slow":
            await asyncio.sleep(2)
        if kind == "redirect":
            return httpx.Response(307, headers={"location": "http://arbitrary.test"})
        if kind == "error":
            return httpx.Response(500, text="private-provider-key")
        if kind == "oversize":
            return httpx.Response(200, text="x" * 1100)
        if kind == "malformed_usage":
            return response(usage=[1])
        return httpx.Response(200, json={"choices": []})

    broker = TargetInferenceBroker(
        configured(target_model_timeout_seconds=1, target_model_max_response_bytes=1024),
        transport=httpx.MockTransport(provider),
    )
    try:
        broker.register("episode-1", broker.profile)
        result = await broker.complete(work(broker))
        assert result.audit.status == ("TIMED_OUT" if kind in {"slow", "timeout"} else "FAILED")
        assert result.audit.usage_source == "conservative_reservation"
        assert result.audit.total_tokens > 1000
        assert "private-provider-key" not in result.model_dump_json()
        assert await broker.complete(work(broker)) == result
        assert len(calls) == 1
    finally:
        await broker.aclose()


async def test_missing_usage_revision_and_provider_echo_are_explicit():
    broker = TargetInferenceBroker(
        configured(target_model_revision="operator-sha256"),
        transport=httpx.MockTransport(
            lambda r: response(
                usage=None, choices=[{"message": {"content": "private-provider-key"}}]
            )
        ),
    )
    try:
        broker.register("episode-1", broker.profile)
        result = await broker.complete(work(broker))
        assert result.content == "[REDACTED]"
        assert result.audit.usage_source == "conservative_reservation"
        assert result.audit.cost_source == "unpriced"
        assert result.audit.reproducibility == "operator_revision_declared"
    finally:
        await broker.aclose()


async def test_mailbox_scoping_idempotency_capacity_and_fail_closed():
    broker = TargetInferenceBroker(
        configured(), transport=httpx.MockTransport(lambda r: response())
    )
    broker.register("episode-1", broker.profile)
    mailbox = InferenceMailbox("episode-1", broker.profile)
    tasks = [asyncio.create_task(mailbox.submit(str(i), work(broker).request)) for i in range(4)]
    try:
        await asyncio.sleep(0)
        with pytest.raises(OverflowError):
            await mailbox.submit("excess", work(broker).request)
        claimed = mailbox.claim()[0]
        with pytest.raises(ValueError, match="different input"):
            await mailbox.submit("0", work(broker, model=broker.profile.model).request)
        result = await broker.complete(claimed)
        with pytest.raises(ValueError, match="match"):
            mailbox.complete(
                result.model_copy(
                    update={"audit": result.audit.model_copy(update={"episode_id": "other"})}
                )
            )
        mailbox.complete(result)
        mailbox.complete(result)
        assert await mailbox.submit("0", work(broker).request) == result
        assert len(mailbox.records) == 1
        mailbox.fail_pending()
        results = await asyncio.gather(*tasks)
        assert results[0].audit.status == "SUCCEEDED"
        assert all(r.audit.usage_source == "not_dispatched" for r in results[1:])
        assert mailbox.claim() == []
        with pytest.raises(ValueError, match="closed"):
            await mailbox.submit("new", work(broker).request)
    finally:
        mailbox.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await broker.aclose()


def test_target_cannot_override_endpoint_or_parameters():
    for field, value in [
        ("base_url", "http://external.test"),
        ("max_tokens", 1000000),
        ("temperature", 2),
        ("api_key", "secret"),
    ]:
        with pytest.raises(ValidationError):
            TargetInferenceRequest.model_validate(
                {"messages": [{"role": "user", "content": "x"}], field: value}
            )


async def test_global_concurrency_limit_and_cancelled_request_cannot_be_redispatched():
    active = peak = 0
    started, release = asyncio.Event(), asyncio.Event()

    async def provider(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.set()
        try:
            await release.wait()
            return response()
        finally:
            active -= 1

    broker = TargetInferenceBroker(
        configured(target_model_max_concurrency=1), transport=httpx.MockTransport(provider)
    )
    for episode in ("episode-1", "episode-2"):
        broker.register(episode, broker.profile)
    first = asyncio.create_task(broker.complete(work(broker)))
    second = None
    try:
        await started.wait()
        second = asyncio.create_task(broker.complete(work(broker, episode_id="episode-2")))
        await asyncio.sleep(0)
        assert peak == 1
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        result = await broker.complete(work(broker))
        assert result.audit.status == "CANCELLED" and result.audit.total_tokens > 0
        release.set()
        assert (await second).audit.status == "SUCCEEDED"
        assert peak == 1
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await broker.aclose()


async def test_oversize_input_and_unclaimed_failure_never_dispatch():
    broker = TargetInferenceBroker(
        configured(target_model_max_input_bytes=256),
        transport=httpx.MockTransport(lambda r: pytest.fail("must not dispatch")),
    )
    broker.register("episode-1", broker.profile)
    try:
        request = TargetInferenceRequest(messages=[InferenceMessage(role="user", content="x" * 300)])
        oversized = work(broker).model_copy(
            update={
                "request": request,
                "request_hash": inference_hash(request.model_dump(mode="json")),
            }
        )
        assert (await broker.complete(oversized)).audit.error_code == "input_limit"
        mailbox = InferenceMailbox("episode-1", broker.profile)
        with pytest.raises(ValueError, match="limit"):
            await mailbox.submit("oversized", request)
        waiting = asyncio.create_task(mailbox.submit("waiting", work(broker).request))
        await asyncio.sleep(0)
        mailbox.fail(mailbox.requests["waiting"], "relay_queue_timeout")
        assert (await waiting).audit.usage_source == "not_dispatched"
        assert mailbox.claim() == []
    finally:
        await broker.aclose()


def test_cli_exports_no_credentials_and_renders_pinned_model_bundle(tmp_path, monkeypatch, capsys):
    import sys

    from adversarial_agent_mvp import bundle_cli
    from adversarial_agent_mvp.scenarios import TargetBundle

    profile_path, bundle_path = tmp_path / "profile.json", tmp_path / "bundle.json"
    monkeypatch.setattr(bundle_cli, "get_settings", configured)
    monkeypatch.setattr(
        sys, "argv", ["adversarial-bundle", "inference-profile", "--output", str(profile_path)]
    )
    bundle_cli.main()
    exported = profile_path.read_text() + capsys.readouterr().out
    assert "private-provider-key" not in exported and "http://operator.test" not in exported
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adversarial-bundle",
            "reference",
            "--image",
            "sha256:" + "a" * 64,
            "--inference-profile",
            str(profile_path),
            "--output",
            str(bundle_path),
        ],
    )
    bundle_cli.main()
    bundle = TargetBundle.model_validate_json(bundle_path.read_text())
    assert bundle.execution_mode == "model"
    assert bundle.manifest.inference_profile == profile_from_settings(configured())
    assert bundle.manifest.entrypoint[-2:] == ["--mode", "model"]
