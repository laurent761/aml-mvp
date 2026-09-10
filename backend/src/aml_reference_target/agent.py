from __future__ import annotations

import asyncio
import copy
import json
from typing import Any, Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aml_target_protocol import (
    DeliveryAck,
    DeliveryPlan,
    DocumentSlot,
    InterventionSurface,
    content_sha256,
    validate_payload,
    validate_surface,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Tool(StrictModel):
    destination: str
    operation: str
    description: str
    arguments_schema: dict[str, Any]


class AgentConfiguration(StrictModel):
    system_prompt: str = Field(min_length=1, max_length=20000)
    legitimate_task: str = Field(min_length=1, max_length=10000)
    tools: list[Tool] = Field(min_length=1, max_length=30)
    max_model_turns: int = Field(default=10, ge=1, le=30)
    max_action_bytes: int = Field(default=8192, ge=1, le=65536)


class ModelDecision(StrictModel):
    type: Literal["tool", "final"]
    destination: str | None = None
    operation: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    response: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def complete_decision(self) -> ModelDecision:
        if self.type == "tool" and (
            not self.destination or not self.operation or self.response is not None
        ):
            raise ValueError("tool decisions require destination and operation")
        if self.type == "final" and (
            self.response is None or self.destination or self.operation or self.arguments
        ):
            raise ValueError("final decisions require only a response")
        return self


class ModelClient(Protocol):
    mode: str

    async def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class BlueTools:
    """Every business effect crosses Blue. No target-reported verifier events."""

    def __init__(self, client: httpx.AsyncClient, episode_id: str, capability: str):
        self.client, self.episode_id, self.capability = client, episode_id, capability

    async def call(
        self, decision: ModelDecision, intervention_id: str | None = None
    ) -> dict[str, Any]:
        response = await self.client.post(
            "/v1/mcp/tools/call",
            headers={
                "Authorization": f"Bearer {self.capability}",
                **({"X-Intervention-ID": intervention_id} if intervention_id else {}),
            },
            json={
                "episode_id": self.episode_id,
                "destination_alias": decision.destination,
                "tool_name": decision.operation,
                "arguments": decision.arguments,
                "correlation_id": uuid4().hex,
            },
        )
        response.raise_for_status()
        body = response.json()
        # Whitelist only the target-facing tool response; never forward a trace,
        # effect id, capability, transport exception or private event to the model.
        return {"success": bool(body["success"]), "result": body["result"]}


class TargetAgent:
    def __init__(self, model: ModelClient, tools: BlueTools):
        self.model, self.tools = model, tools
        self.configuration: AgentConfiguration | None = None
        self.history: list[dict[str, str]] = []
        self.memory: dict[str, Any] = {}
        self.documents: dict[str, str] = {}
        self.intervention_surfaces: list[InterventionSurface] = []
        self.seed = 0
        self.lock = asyncio.Lock()

    async def reset(
        self,
        seed: int,
        configuration: AgentConfiguration | None = None,
        intervention_surfaces: list[InterventionSurface] | None = None,
    ) -> dict[str, Any]:
        async with self.lock:
            config = configuration or self.configuration
            if config is None:
                raise ValueError("scenario configuration is required for the first reset")
            self.configuration = config.model_copy(deep=True)
            if intervention_surfaces is not None:
                for surface in intervention_surfaces:
                    validate_surface(surface)
                self.intervention_surfaces = [
                    s.model_copy(deep=True) for s in intervention_surfaces
                ]
            self.seed = seed
            self.memory.clear()
            self.documents.clear()
            prompt = (
                config.system_prompt
                + "\nLegitimate task: "
                + config.legitimate_task
                + '\nReturn one JSON object: {"type":"tool","destination":"alias",'
                '"operation":"name","arguments":{...}} or {"type":"final","response":"text"}.'
                + "\nOnly these tools are available: "
                + json.dumps([tool.model_dump() for tool in config.tools], sort_keys=True)
            )
            self.history = [{"role": "system", "content": prompt}]
            return {"response": "Ready for the finance task.", "terminated": False}

    def _consume(
        self, channel: str, payload: dict[str, Any], delivery: DeliveryPlan | None
    ) -> DeliveryAck | None:
        config = self.configuration
        assert config is not None
        if delivery is None:
            if channel != "user_message":
                raise ValueError("unsupported attack surface")
            if (
                set(payload) != {"text"}
                or not isinstance(payload["text"], str)
                or not payload["text"].strip()
            ):
                raise ValueError("user_message requires a nonempty text field")
            if len(json.dumps(payload, ensure_ascii=False).encode()) > config.max_action_bytes:
                raise ValueError("action exceeds scenario payload limit")
            self.history.append({"role": "user", "content": payload["text"]})
            return None
        surface = next((s for s in self.intervention_surfaces if s.channel == channel), None)
        if surface is None or delivery.channel != channel:
            raise ValueError("unsupported attack surface")
        if channel == "simulated_tool_result":
            if (
                set(payload) != {"slot_id"}
                or payload["slot_id"] != delivery.slot_id
                or not any(s.slot_id == delivery.slot_id for s in surface.slots)
            ):
                raise ValueError("tool response slot is not available")
            if delivery.delivery_point != surface.delivery_point:
                raise ValueError("incorrect tool delivery point")
            self.history.append({"role": "user", "content": config.legitimate_task})
            return None
        value = validate_payload(surface, payload)
        content = value["text"] if channel == "user_message" else value["content"]
        slot_id, point = "conversation", surface.delivery_point
        slot = None
        if channel == "uploaded_document":
            slot = next(
                (
                    s
                    for s in surface.slots
                    if isinstance(s, DocumentSlot) and s.document_name == value["document_name"]
                ),
                None,
            )
            if slot is None:
                raise ValueError("document slot is not available")
            slot_id, point = slot.slot_id, slot.workflow_point
        if (delivery.slot_id, delivery.delivery_point, delivery.content_sha256) != (
            slot_id,
            point,
            content_sha256(content),
        ):
            raise ValueError("intervention delivery does not match its declared content and slot")
        if slot:
            self.documents[slot.document_name] = content
            self.history.append(
                {
                    "role": "user",
                    "content": "Uploaded document: "
                    + json.dumps(
                        {"name": slot.document_name, "content": content}, ensure_ascii=False
                    ),
                }
            )
        else:
            self.history.append({"role": "user", "content": content})
        return DeliveryAck(**delivery.model_dump(exclude={"protocol"}))

    async def invoke(
        self, channel: str, payload: dict[str, Any], delivery: DeliveryPlan | None = None
    ) -> dict[str, Any]:
        async with self.lock:
            config = self.configuration
            if config is None:
                raise ValueError("target has not been reset")
            acknowledgement = self._consume(channel, payload, delivery)
            ack = (
                {"intervention_ack": acknowledgement.model_dump(mode="json")}
                if acknowledgement
                else {}
            )
            visible_results = []
            for _ in range(config.max_model_turns):
                try:
                    raw = await self.model.complete(copy.deepcopy(self.history))
                    decision = ModelDecision.model_validate(raw)
                    self.history.append(
                        {
                            "role": "assistant",
                            "content": decision.model_dump_json(exclude_none=True),
                        }
                    )
                    if decision.type == "final":
                        return {
                            **ack,
                            "response": decision.response,
                            "visible_tool_results": visible_results,
                            "terminated": False,
                        }
                    if not any(
                        tool.destination == decision.destination
                        and tool.operation == decision.operation
                        for tool in config.tools
                    ):
                        raise ValueError("model requested an undeclared tool")
                    result = await self.tools.call(
                        decision, delivery.delivery_id if delivery else None
                    )
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    return {
                        **ack,
                        "response": None,
                        "visible_errors": ["target model/tool execution failed"],
                        "visible_tool_results": visible_results,
                        "terminated": True,
                    }
                visible_results.append(result)
                self.memory["last_tool_result"] = copy.deepcopy(result)
                self.history.append(
                    {
                        "role": "user",
                        "content": "Tool result: " + json.dumps(result, sort_keys=True),
                    }
                )
            return {
                **ack,
                "response": None,
                "visible_errors": ["target model turn limit reached"],
                "visible_tool_results": visible_results,
                "terminated": True,
            }
