import copy
import json

import pytest

from adversarial_agent_mvp.bundle_cli import reference_bundle
from adversarial_agent_mvp.bundle_smoke import open_bundle
from adversarial_agent_mvp.contracts import EffectAttempt, PublicEffectResponse
from adversarial_agent_mvp.interventions import InterventionDelivery
from adversarial_agent_mvp.scenarios import PublicScenario, TargetBundle, content_hash
from aml_target_protocol import (
    DeliveryReceipt,
    InterventionSurface,
    ToolResultSlot,
    validate_payload,
    validate_surface,
)


def bundle():
    return reference_bundle("sha256:" + "a" * 64)


@pytest.mark.parametrize(
    "change", ["point", "schema", "route", "slot_kind", "duplicate", "private_path"]
)
def test_catalog_rejects_unsupported_or_unbounded_slot_definitions(change):
    document = bundle().model_dump(mode="json")
    surface = document["scenario"]["surfaces"][2]
    if change == "point":
        surface["delivery_point"] = "before_any_message"
    elif change == "schema":
        surface["payload_schema"]["additionalProperties"] = True
    elif change == "route":
        surface["slots"][0]["operation"] = "payment.create"
    elif change == "slot_kind":
        surface["slots"] = document["scenario"]["surfaces"][1]["slots"]
    elif change == "duplicate":
        surface["slots"].append(copy.deepcopy(surface["slots"][0]))
    else:
        surface["slots"][0]["result_path"] = ["private_events"]
    with pytest.raises(ValueError):
        TargetBundle.model_validate(document)


def test_payload_byte_boundary_is_utf8_and_matches_published_schema():
    surface = bundle().scenario.surfaces[1]
    payload = {"document_name": "invoice-note.txt", "content": "こんにちは"}
    size = len(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    exact = InterventionSurface.model_validate({**surface.model_dump(), "max_payload_bytes": size})
    assert validate_payload(exact, payload) == payload
    with pytest.raises(ValueError, match="limit"):
        validate_payload(exact.model_copy(update={"max_payload_bytes": size - 1}), payload)
    validate_surface(exact)


def test_response_slot_occurrence_and_missing_or_non_string_leaf_fail_closed():
    surface = bundle().scenario.surfaces[2]
    definition = surface.model_dump()
    definition["slots"][0]["occurrence"] = 2
    delivery = InterventionDelivery(
        "e", "scenario", [InterventionSurface.model_validate(definition)]
    )
    attempt = delivery.prepare(
        {
            "action_id": "a",
            "channel": "simulated_tool_result",
            "payload": {"slot_id": "invoice-content", "content": "replacement"},
        }
    )

    def effect(index):
        return EffectAttempt(
            episode_id="e",
            protocol="mcp",
            destination_alias="documents",
            operation="file.read",
            arguments={"file_id": "invoice-001"},
            correlation_id=str(index),
        )

    public = PublicEffectResponse(
        effect_id="effect",
        success=True,
        result={"file": {"content": "original", "owner": "trusted"}},
    )
    assert delivery.overlay(effect(1), public, attempt.plan.delivery_id) == public
    changed = delivery.overlay(effect(2), public, attempt.plan.delivery_id)
    assert changed.result == {"file": {"content": "replacement", "owner": "trusted"}}
    assert public.result["file"]["content"] == "original"
    assert delivery.overlay(effect(3), public, attempt.plan.delivery_id) == public
    assert delivery.finish(attempt).applied
    for result in ({}, {"file": {"content": ["not a string"]}}):
        controller = InterventionDelivery("e", "scenario", [surface])
        pending = controller.prepare(
            {
                "action_id": "a",
                "channel": "simulated_tool_result",
                "payload": {"slot_id": "invoice-content", "content": "replacement"},
            }
        )
        original = public.model_copy(update={"result": result})
        assert controller.overlay(effect(1), original, pending.plan.delivery_id) == original
        assert controller.finish(pending).status == "unavailable_slot"


async def test_legacy_scenario_hashes_and_message_targets_remain_compatible():
    document = bundle().model_dump(mode="json")
    document["manifest"].pop("intervention_protocol")
    document["scenario"]["version"] = "1.0.0"
    document["scenario"]["surfaces"] = document["scenario"]["surfaces"][:1]
    assert "slots" not in document["scenario"]["surfaces"][0]
    original_hash = content_hash(document["scenario"])
    assert (
        content_hash(PublicScenario.model_validate(document["scenario"]).model_dump(mode="json"))
        == original_hash
    )
    legacy = TargetBundle.model_validate(document)
    async with open_bundle(legacy) as session:
        result = await session.environment.step(legacy.ground_truth.benign_actions[0])
        assert not result.public_observation.visible_errors
        assert result.public_observation.delivery_receipt is None


def test_receipts_cannot_claim_applied_without_exact_content_or_evidence():
    delivery = InterventionDelivery("e", "scenario", bundle().scenario.surfaces)
    attempt = delivery.prepare(
        {
            "action_id": "a",
            "channel": "uploaded_document",
            "payload": {"document_name": "missing.txt", "content": "hello"},
        }
    )
    raw = attempt.receipt.model_dump()
    for change in (
        {"applied": True},
        {"content_sha256": "a" * 64},
        {"delivered_content": "not delivered"},
    ):
        with pytest.raises(ValueError):
            DeliveryReceipt.model_validate({**raw, **change})
    with pytest.raises(ValueError):
        ToolResultSlot(
            slot_id="bad",
            destination_alias="documents",
            operation="file.read",
            arguments_match={},
            result_path=["file", "content"],
        )
