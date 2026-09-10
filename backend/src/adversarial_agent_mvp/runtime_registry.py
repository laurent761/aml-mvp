"""Operator-approved, checkpoint-attested HTTP attacker clients; no strategy memory."""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from .contracts import RedAction
from .research_contracts import RuntimeCreate
from .scenarios import content_hash


class RegisteredAttacker:
    def __init__(self, document: dict[str, Any], *, client: httpx.AsyncClient | None = None):
        self.config = RuntimeCreate.model_validate(document)
        self.client = client or httpx.AsyncClient(timeout=self.config.timeout_seconds,
                                                 trust_env=False, follow_redirects=False)
        self.owns_client = client is None
        self.last_generation: dict[str, Any] | None = None
        self.last_usage: dict[str, Any] | None = None

    def headers(self) -> dict[str, str]:
        if not self.config.credential_ref:
            return {}
        key = os.environ.get(self.config.credential_ref)
        if not key:
            raise RuntimeError("registered runtime credential is unavailable")
        return {"Authorization": f"Bearer {key}"}

    async def request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self.client.stream("GET" if body is None else "POST",
                self.config.endpoint.rstrip("/") + path, json=body, headers=self.headers()) as response:
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 1048576:
                    raise RuntimeError("runtime response exceeded its byte limit")
            if body is not None and self.last_generation is not None:
                self.last_generation["raw_response"] = chunks.decode("utf-8", errors="replace")
            response.raise_for_status()
        result = json.loads(chunks)
        if body is not None and self.last_generation is not None:
            self.last_generation["raw_response"] = result
        if not isinstance(result, dict) or result.get("protocol") != "aml.attacker.v1" or result.get("checkpoint_id") != self.config.checkpoint_id or result.get("model") != self.config.model:
            raise RuntimeError("runtime identity changed or failed checkpoint attestation")
        return result

    async def health(self) -> dict[str, Any]:
        result = await self.request("/health")
        if result.get("status") != "ready":
            raise RuntimeError("registered runtime is not ready")
        return {"status": "ready", "checkpoint_id": result["checkpoint_id"],
                "model": result["model"], "identity_source": "runtime_attestation"}

    def reservation(self) -> tuple[int, float]:
        tokens = self.config.max_input_bytes + 1024 + self.config.max_output_tokens
        cost = ((self.config.max_input_bytes + 1024) * self.config.input_cost_per_million +
                self.config.max_output_tokens * self.config.output_cost_per_million) / 1000000
        return tokens, cost

    async def propose(self, public: dict[str, Any], seed: int) -> RedAction:
        self.last_generation, self.last_usage = None, None
        body = {"protocol": "aml.attacker.v1", "checkpoint_id": self.config.checkpoint_id,
                "model": self.config.model, "public_context": public, "seed": seed,
                "generation_config": self.config.generation_config,
                "max_output_tokens": self.config.max_output_tokens}
        self.last_generation = {"prompt_messages": None, "public_history": public.get("observations"),
            "raw_response": None, "parsed_action": None, "parsing_error": None,
            "generation_config": self.config.generation_config, "seed": seed,
            "checkpoint_id": self.config.checkpoint_id, "tokenizer": None, "template": None,
            "request": body}
        encoded = json.dumps(body, sort_keys=True).encode()
        if len(encoded) > self.config.max_input_bytes:
            self.last_generation["parsing_error"] = "runtime input exceeded its byte limit; no request sent"
            raise RuntimeError("runtime input exceeded its byte limit")
        started = time.monotonic()
        reserved_tokens, reserved_cost = self.reservation()
        self.last_usage = {"tokens": reserved_tokens, "cost": reserved_cost,
            "input_tokens": self.config.max_input_bytes + 1024,
            "output_tokens": self.config.max_output_tokens,
            "usage_source": "conservative_reservation", "request_hash": content_hash(body),
            "cost_source": "unpriced" if reserved_cost == 0 else "operator_rates"}
        try:
            response = await self.request("/generate", body)
            self.last_generation["raw_response"] = response
            usage = response.get("usage", {})
            input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
            if (type(input_tokens) is int and type(output_tokens) is int
                    and 0 <= input_tokens <= self.config.max_input_bytes + 1024
                    and 0 <= output_tokens <= self.config.max_output_tokens):
                self.last_usage.update(tokens=input_tokens + output_tokens, input_tokens=input_tokens,
                    output_tokens=output_tokens, usage_source="runtime_reported",
                    cost=(input_tokens * self.config.input_cost_per_million + output_tokens * self.config.output_cost_per_million) / 1000000)
            action = RedAction.model_validate(response["action"])
            self.last_generation["parsed_action"] = action.model_dump(mode="json")
            return action
        except Exception:
            self.last_generation["parsing_error"] = "runtime generation or identity validation failed"
            raise
        finally:
            self.last_usage["latency_ms"] = int((time.monotonic() - started) * 1000)

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()
