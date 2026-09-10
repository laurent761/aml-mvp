"""Public intervention contract shared by opaque targets and the trusted gateway."""

from .interventions import (
    DeliveryAck,
    DeliveryPlan,
    DeliveryReceipt,
    DocumentSlot,
    InterventionAction,
    InterventionSurface,
    ToolResultSlot,
    content_sha256,
    payload_schema,
    validate_payload,
    validate_surface,
)

__all__ = [
    "DeliveryAck",
    "DeliveryPlan",
    "DeliveryReceipt",
    "DocumentSlot",
    "InterventionAction",
    "InterventionSurface",
    "ToolResultSlot",
    "content_sha256",
    "payload_schema",
    "validate_payload",
    "validate_surface",
]
