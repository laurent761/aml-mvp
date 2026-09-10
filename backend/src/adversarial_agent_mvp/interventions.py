"""Trusted delivery validation and response overlays, separate from world truth."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from aml_target_protocol import (
    DeliveryAck,
    DeliveryPlan,
    DeliveryReceipt,
    DocumentSlot,
    InterventionAction,
    InterventionSurface,
    ToolResultSlot,
    content_sha256,
    validate_payload,
    validate_surface,
)

from .contracts import EffectAttempt, PublicEffectResponse, new_id


def payload_hash(payload: Any) -> str:
    return content_sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    )


def _safe_identifier(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    try:
        value.encode("utf-8")
    except UnicodeError:
        return None
    return value


@dataclass
class DeliveryAttempt:
    fields: dict[str, Any]
    action: InterventionAction | None = None
    plan: DeliveryPlan | None = None
    slot: DocumentSlot | ToolResultSlot | None = None
    content: str | None = None
    matches: int = 0
    receipt: DeliveryReceipt | None = None

    def target_body(self) -> dict[str, Any]:
        assert self.action is not None and self.plan is not None
        payload = self.action.payload
        if self.action.channel == "simulated_tool_result":
            # The replacement is held only by Blue until its declared response
            # slot is reached. It must never enter the conversational input.
            payload = {"slot_id": self.plan.slot_id}
        return {
            "action_id": self.action.action_id,
            "channel": self.action.channel,
            "payload": payload,
            "delivery": self.plan.model_dump(mode="json"),
        }


class InterventionDelivery:
    def __init__(
        self, episode_id: str, scenario_version_id: str | None, surfaces: list[InterventionSurface]
    ):
        for surface in surfaces:
            validate_surface(surface)
        if len({s.channel for s in surfaces}) != len(surfaces):
            raise ValueError("intervention channels must be unique")
        self.episode_id, self.scenario_version_id = episode_id, scenario_version_id
        self.surfaces = {s.channel: s.model_copy(deep=True) for s in surfaces}
        self.active: DeliveryAttempt | None = None
        self.records: list[dict[str, Any]] = []

    def _record(
        self,
        attempt: DeliveryAttempt,
        status: str,
        *,
        reason: str | None = None,
        evidence: str = "none",
        effect: EffectAttempt | None = None,
    ) -> DeliveryReceipt:
        if attempt.receipt is not None:
            return attempt.receipt
        applied = status == "applied"
        receipt = DeliveryReceipt.model_validate(
            {
                **attempt.fields,
                "status": status,
                "applied": applied,
                "reason": reason,
                "evidence": evidence,
                "slot_id": attempt.plan.slot_id if applied and attempt.plan else None,
                "delivery_point": attempt.plan.delivery_point if attempt.plan else None,
                "delivered_content": attempt.content if applied else None,
                "content_sha256": content_sha256(attempt.content)
                if applied and attempt.content is not None
                else None,
                "effect_id": effect.effect_id if effect else None,
                "tool_correlation_id": effect.correlation_id if effect else None,
            }
        )
        attempt.receipt = receipt
        self.records.append({"sequence": len(self.records) + 1, **receipt.model_dump(mode="json")})
        return receipt

    def prepare(self, document: Any, *, malformed_reason: str | None = None) -> DeliveryAttempt:
        if self.active is not None:
            raise RuntimeError("a delivery is already active")
        if len(self.records) >= 1000:
            raise RuntimeError("intervention receipt limit reached")
        raw = document if isinstance(document, dict) else {}
        try:
            fingerprint = payload_hash(raw.get("payload", document))
        except (ValueError, TypeError, RecursionError):
            fingerprint = content_sha256("invalid-json")
            malformed_reason = malformed_reason or "invalid_json"
        attempt = DeliveryAttempt(
            fields={
                "receipt_id": new_id("delivery"),
                "episode_id": self.episode_id,
                "scenario_version_id": self.scenario_version_id,
                "action_id": _safe_identifier(raw.get("action_id"), 200),
                "channel": _safe_identifier(raw.get("channel"), 100),
                "payload_sha256": fingerprint,
            }
        )
        try:
            if (
                malformed_reason
                or attempt.fields["action_id"] is None
                or attempt.fields["channel"] is None
            ):
                raise ValueError(malformed_reason)
            action = InterventionAction.model_validate(document)
        except (ValueError, TypeError):
            self._record(
                attempt, "malformed_action", reason=malformed_reason or "invalid_action_envelope"
            )
            return attempt
        attempt.action = action
        surface = self.surfaces.get(action.channel)
        if surface is None:
            self._record(attempt, "unsupported_surface", reason="surface_not_declared")
            return attempt
        try:
            payload = validate_payload(surface, action.payload)
        except (ValueError, TypeError, RecursionError):
            self._record(attempt, "malformed_action", reason="invalid_payload_or_size")
            return attempt
        slot_id, point = "conversation", surface.delivery_point
        if action.channel == "uploaded_document":
            attempt.fields["requested_slot"] = payload["document_name"]
            attempt.slot = next(
                (
                    s
                    for s in surface.slots
                    if isinstance(s, DocumentSlot) and s.document_name == payload["document_name"]
                ),
                None,
            )
        elif action.channel == "simulated_tool_result":
            attempt.fields["requested_slot"] = payload["slot_id"]
            attempt.slot = next((s for s in surface.slots if s.slot_id == payload["slot_id"]), None)
        else:
            attempt.fields["requested_slot"] = "conversation"
        if action.channel != "user_message" and attempt.slot is None:
            self._record(attempt, "unavailable_slot", reason="slot_not_declared")
            return attempt
        if attempt.slot:
            slot_id = attempt.slot.slot_id
            if isinstance(attempt.slot, DocumentSlot):
                point = attempt.slot.workflow_point
        attempt.content = (
            payload["text"] if action.channel == "user_message" else payload["content"]
        )
        assert attempt.content is not None
        attempt.plan = DeliveryPlan(
            delivery_id=attempt.fields["receipt_id"],
            channel=surface.channel,
            slot_id=slot_id,
            delivery_point=point,
            content_sha256=content_sha256(attempt.content),
        )
        self.active = attempt
        return attempt

    def overlay(
        self, effect: EffectAttempt, public: PublicEffectResponse, delivery_id: str | None
    ) -> PublicEffectResponse:
        attempt = self.active
        if (
            attempt is None
            or attempt.receipt is not None
            or attempt.plan is None
            or attempt.plan.delivery_id != delivery_id
        ):
            return public
        slot = attempt.slot
        if not isinstance(slot, ToolResultSlot) or (slot.destination_alias, slot.operation) != (
            effect.destination_alias,
            effect.operation,
        ):
            return public
        if any(
            key not in effect.arguments
            or payload_hash(effect.arguments[key]) != payload_hash(value)
            for key, value in slot.arguments_match.items()
        ):
            return public
        attempt.matches += 1
        if attempt.matches != slot.occurrence or not public.success:
            return public
        result = copy.deepcopy(public.result)
        parent: Any = result
        for key in slot.result_path[:-1]:
            if not isinstance(parent, dict) or key not in parent:
                return public
            parent = parent[key]
        if not isinstance(parent, dict) or not isinstance(parent.get(slot.result_path[-1]), str):
            return public
        parent[slot.result_path[-1]] = attempt.content
        self._record(attempt, "applied", evidence="gateway_tool_response", effect=effect)
        return public.model_copy(update={"result": result})

    def finish(
        self,
        attempt: DeliveryAttempt,
        body: dict[str, Any] | None = None,
        *,
        failure: str | None = None,
        reason: str | None = None,
    ) -> DeliveryReceipt:
        try:
            if attempt.receipt:
                return attempt.receipt
            if failure:
                return self._record(
                    attempt,
                    failure,
                    reason=reason,
                    evidence="unconfirmed" if failure == "delivery_unknown" else "none",
                )
            if attempt.action and attempt.action.channel == "simulated_tool_result":
                return self._record(
                    attempt, "unavailable_slot", reason="declared_response_slot_not_observed"
                )
            try:
                ack = DeliveryAck.model_validate((body or {}).get("intervention_ack"))
                if attempt.plan is None or ack.model_dump() != attempt.plan.model_dump(
                    exclude={"protocol"}
                ):
                    raise ValueError("mismatched acknowledgement")
            except ValueError:
                return self._record(
                    attempt,
                    "delivery_unknown",
                    reason="missing_or_invalid_target_acknowledgement",
                    evidence="unconfirmed",
                )
            return self._record(attempt, "applied", evidence="target_acknowledgement")
        finally:
            if self.active is attempt:
                self.active = None
