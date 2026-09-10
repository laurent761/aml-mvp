from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)

Channel = Literal["user_message", "uploaded_document", "simulated_tool_result"]
SlotId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class DocumentSlot(Contract):
    kind: Literal["document"] = "document"
    slot_id: SlotId
    document_name: SlotId
    workflow_point: Literal["before_first_model_turn"] = "before_first_model_turn"


class ToolResultSlot(Contract):
    kind: Literal["tool_result"] = "tool_result"
    slot_id: SlotId
    destination_alias: SlotId
    operation: str = Field(min_length=1, max_length=100)
    arguments_match: dict[str, Any] = Field(min_length=1, max_length=30)
    occurrence: int = Field(default=1, ge=1, le=30)
    result_path: list[SlotId] = Field(min_length=1, max_length=10)

    @field_validator("result_path")
    @classmethod
    def public_data_only(cls, path: list[str]) -> list[str]:
        if set(path) & {
            "private_events",
            "verifier",
            "verifier_signals",
            "effect_id",
            "success",
            "identity",
            "trusted_context",
        }:
            raise ValueError("interventions may replace only public result content")
        return path


class InterventionSurface(Contract):
    channel: Channel
    description: str = Field(min_length=1)
    delivery_point: str = Field(min_length=1)
    max_payload_bytes: int = Field(ge=1, le=1_000_000)
    payload_schema: dict[str, Any]
    slots: list[Annotated[DocumentSlot | ToolResultSlot, Field(discriminator="kind")]] = Field(
        default_factory=list, max_length=30
    )

    @model_serializer(mode="wrap")
    def serialize(self, handler: Any) -> dict[str, Any]:
        document = handler(self)
        # Preserve hashes of historical scenarios that predate declared slots.
        if not self.slots:
            document.pop("slots", None)
        return document


class InterventionAction(Contract):
    action_id: StrictStr = Field(min_length=1, max_length=200)
    channel: StrictStr = Field(min_length=1, max_length=100)
    payload: dict[str, Any]


class UserMessagePayload(Contract):
    text: StrictStr = Field(min_length=1)


class UploadedDocumentPayload(Contract):
    document_name: SlotId
    content: StrictStr = Field(min_length=1)


class ToolResultPayload(Contract):
    slot_id: SlotId
    content: StrictStr = Field(min_length=1)


def payload_schema(channel: Channel) -> dict[str, Any]:
    keys = {
        "user_message": ["text"],
        "uploaded_document": ["document_name", "content"],
        "simulated_tool_result": ["slot_id", "content"],
    }[channel]
    properties: dict[str, Any] = {key: {"type": "string", "minLength": 1} for key in keys}
    for key in ("document_name", "slot_id"):
        if key in properties:
            properties[key].update(
                {"maxLength": 128, "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"}
            )
    return {
        "type": "object",
        "properties": properties,
        "required": keys,
        "additionalProperties": False,
    }


def validate_surface(surface: InterventionSurface) -> None:
    points = {
        "user_message": "next_conversation_turn",
        "uploaded_document": "workflow_document",
        "simulated_tool_result": "tool_response",
    }
    if surface.delivery_point != points[
        surface.channel
    ] or surface.payload_schema != payload_schema(surface.channel):
        raise ValueError("surface must use its supported delivery point and payload schema")
    if surface.channel == "user_message":
        if surface.slots:
            raise ValueError("conversation delivery cannot declare document/tool slots")
        return
    if not surface.slots or len({slot.slot_id for slot in surface.slots}) != len(surface.slots):
        raise ValueError("surface requires unique declared slots")
    expected = DocumentSlot if surface.channel == "uploaded_document" else ToolResultSlot
    if any(not isinstance(slot, expected) for slot in surface.slots):
        raise ValueError("slot kind does not match its surface")
    if surface.channel == "uploaded_document" and len(
        {slot.document_name for slot in surface.slots if isinstance(slot, DocumentSlot)}
    ) != len(surface.slots):
        raise ValueError("document names must be unique")


def validate_payload(surface: InterventionSurface, payload: dict[str, Any]) -> dict[str, Any]:
    model = {
        "user_message": UserMessagePayload,
        "uploaded_document": UploadedDocumentPayload,
        "simulated_tool_result": ToolResultPayload,
    }[surface.channel]
    value = model.model_validate(payload).model_dump()
    if any(not item.strip() for item in value.values()):
        raise ValueError("intervention strings must not be blank")
    if (
        len(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        )
        > surface.max_payload_bytes
    ):
        raise ValueError("intervention exceeds the scenario payload limit")
    return value


class DeliveryPlan(Contract):
    protocol: Literal["aml.intervention.v1"] = "aml.intervention.v1"
    delivery_id: str = Field(min_length=1, max_length=100)
    channel: Channel
    slot_id: str
    delivery_point: str
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class DeliveryAck(Contract):
    delivery_id: str
    channel: Channel
    slot_id: str
    delivery_point: str
    content_sha256: str


class DeliveryReceipt(Contract):
    protocol: Literal["aml.intervention.v1"] = "aml.intervention.v1"
    receipt_id: str
    episode_id: str
    scenario_version_id: str | None
    action_id: str | None
    channel: str | None
    status: Literal[
        "applied",
        "unsupported_surface",
        "malformed_action",
        "unavailable_slot",
        "target_rejected",
        "delivery_unknown",
    ]
    applied: bool
    requested_slot: str | None = None
    slot_id: str | None = None
    delivery_point: str | None = None
    payload_sha256: str
    content_sha256: str | None = None
    delivered_content: str | None = None
    evidence: Literal["target_acknowledgement", "gateway_tool_response", "none", "unconfirmed"] = (
        "none"
    )
    reason: str | None = None
    effect_id: str | None = None
    tool_correlation_id: str | None = None

    def semantic_document(self) -> dict[str, Any]:
        """Stable delivery facts for replay and novelty, excluding run identities."""
        return self.model_dump(
            mode="json",
            exclude={"receipt_id", "episode_id", "action_id", "effect_id", "tool_correlation_id"},
        )

    @model_validator(mode="after")
    def valid_receipt(self) -> DeliveryReceipt:
        if self.applied != (self.status == "applied"):
            raise ValueError("receipt application status is inconsistent")
        if self.applied:
            if (
                self.delivered_content is None
                or content_sha256(self.delivered_content) != self.content_sha256
                or not self.slot_id
            ):
                raise ValueError("applied receipt must identify its slot and exact content")
            if self.evidence not in {"target_acknowledgement", "gateway_tool_response"}:
                raise ValueError("applied receipt requires delivery evidence")
        elif self.delivered_content is not None or self.content_sha256 is not None:
            raise ValueError("unapplied receipt cannot claim delivered content")
        return self
