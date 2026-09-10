"""Trusted target inference broker, owned exclusively by the capsule supervisor.

There is no generic proxy endpoint. The caller supplies messages; the operator
supplies the destination, model, generation parameters, credentials and limits.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from .inference_contracts import (
    InferenceAudit,
    InferenceResult,
    InferenceWork,
    TargetInferenceProfile,
    inference_hash,
)
from .settings import Settings


def profile_from_settings(settings: Settings) -> TargetInferenceProfile:
    if settings.target_model_provider == "disabled":
        raise ValueError("target inference is disabled")
    endpoint = urlsplit(settings.target_model_base_url or "")
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError(
            "TARGET_MODEL_BASE_URL must be an operator endpoint without credentials or query parameters"
        )
    if settings.target_model_provider == "hosted_openai_compatible":
        if endpoint.scheme != "https" or not settings.target_model_api_key:
            raise ValueError("hosted target inference requires HTTPS and TARGET_MODEL_API_KEY")
    if not settings.target_model_name:
        raise ValueError("TARGET_MODEL_NAME is required")
    return TargetInferenceProfile(
        provider=settings.target_model_provider,
        model=settings.target_model_name,
        endpoint_sha256=inference_hash(str(settings.target_model_base_url).rstrip("/")),
        declared_revision=settings.target_model_revision,
        temperature=settings.target_model_temperature,
        top_p=settings.target_model_top_p,
        max_output_tokens=settings.target_model_max_output_tokens,
        token_parameter=settings.target_model_token_parameter,
        json_mode=settings.target_model_json_mode,
        timeout_seconds=settings.target_model_timeout_seconds,
        max_input_bytes=settings.target_model_max_input_bytes,
        max_response_bytes=settings.target_model_max_response_bytes,
        max_requests=settings.target_model_max_requests,
        max_total_tokens=settings.target_model_max_total_tokens,
        max_cost=settings.target_model_max_cost,
        input_cost_per_million=settings.target_model_input_cost_per_million,
        output_cost_per_million=settings.target_model_output_cost_per_million,
    )


@dataclass
class BrokerEpisode:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    records: dict[str, InferenceResult] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    requests: int = 0
    tokens: int = 0
    cost: float = 0
    closed: bool = False


class TargetInferenceBroker:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.profile = profile_from_settings(settings)
        self._key = (
            settings.target_model_api_key.get_secret_value()
            if settings.target_model_api_key
            else None
        )
        self._client = httpx.AsyncClient(
            base_url=str(settings.target_model_base_url).rstrip("/") + "/",
            timeout=self.profile.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._slots = asyncio.Semaphore(settings.target_model_max_concurrency)
        self._episodes: dict[str, BrokerEpisode] = {}

    def register(self, episode_id: str, profile: TargetInferenceProfile) -> None:
        if profile != self.profile:
            raise ValueError("target inference profile does not match the operator configuration")
        if episode_id in self._episodes:
            raise ValueError("inference episode is already registered")
        self._episodes[episode_id] = BrokerEpisode()

    async def remove(self, episode_id: str) -> None:
        episode = self._episodes.pop(episode_id, None)
        if episode:
            episode.closed = True

    async def aclose(self) -> None:
        for episode in self._episodes.values():
            episode.closed = True
        self._episodes.clear()
        await self._client.aclose()

    def _result(
        self,
        work: InferenceWork,
        *,
        status: Any,
        code: str | None = None,
        content: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0,
        usage_source: Any = "not_dispatched",
        latency_ms: int = 0,
        resolved_model: str | None = None,
        provider_request_id: str | None = None,
        fingerprint: str | None = None,
    ) -> InferenceResult:
        return InferenceResult(
            request_id=work.request_id,
            content=content,
            audit=InferenceAudit(
                request_id=work.request_id,
                episode_id=work.episode_id,
                request_hash=work.request_hash,
                profile=self.profile,
                status=status,
                error_code=code,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                usage_source=usage_source,
                cost=cost,
                cost_source="operator_rates"
                if self.profile.input_cost_per_million or self.profile.output_cost_per_million
                else "unpriced",
                latency_ms=latency_ms,
                resolved_model=resolved_model,
                provider_request_id=provider_request_id,
                system_fingerprint=fingerprint,
                reproducibility="operator_revision_declared"
                if self.profile.declared_revision
                else "provider_revision_unavailable",
            ),
        )

    async def complete(self, work: InferenceWork) -> InferenceResult:
        episode = self._episodes.get(work.episode_id)
        if episode is None or episode.closed:
            raise ValueError("inference episode is not active")
        if (
            work.profile_sha256 != self.profile.sha256
            or inference_hash(work.request.model_dump(mode="json")) != work.request_hash
        ):
            raise ValueError("inference request/profile integrity check failed")
        async with episode.lock:
            if work.request_id in episode.hashes:
                if episode.hashes[work.request_id] != work.request_hash:
                    raise ValueError("inference correlation ID was reused with different input")
                return episode.records[work.request_id]
            p = self.profile
            size = len(
                json.dumps(
                    [m.model_dump() for m in work.request.messages], ensure_ascii=False
                ).encode()
            )
            # Conservative reservation, not a claim about a provider's tokenizer.
            reserve_input = size + 64 * len(work.request.messages) + 256
            reserve_output = p.max_output_tokens
            reserve_cost = (
                reserve_input * p.input_cost_per_million
                + reserve_output * p.output_cost_per_million
            ) / 1_000_000
            rejected = None
            if work.request.model not in {None, p.model}:
                rejected = "model_not_allowed"
            elif size > p.max_input_bytes:
                rejected = "input_limit"
            elif episode.requests >= p.max_requests:
                rejected = "request_budget"
            elif episode.tokens + reserve_input + reserve_output > p.max_total_tokens:
                rejected = "token_budget"
            elif episode.cost + reserve_cost > p.max_cost:
                rejected = "cost_budget"
            if rejected:
                result = self._result(work, status="REJECTED", code=rejected)
            else:
                episode.requests += 1
                episode.tokens += reserve_input + reserve_output
                episode.cost += reserve_cost
                started = time.monotonic()
                try:
                    result = await asyncio.wait_for(
                        self._dispatch(work, reserve_input, reserve_output, reserve_cost),
                        p.timeout_seconds,
                    )
                except TimeoutError:
                    result = self._result(
                        work,
                        status="TIMED_OUT",
                        code="provider_timeout",
                        input_tokens=reserve_input,
                        output_tokens=reserve_output,
                        cost=reserve_cost,
                        usage_source="conservative_reservation",
                    )
                except asyncio.CancelledError:
                    result = self._result(
                        work,
                        status="CANCELLED",
                        code="inference_cancelled",
                        input_tokens=reserve_input,
                        output_tokens=reserve_output,
                        cost=reserve_cost,
                        usage_source="conservative_reservation",
                    )
                    episode.hashes[work.request_id], episode.records[work.request_id] = (
                        work.request_hash,
                        result,
                    )
                    raise
                result = result.model_copy(
                    update={
                        "audit": result.audit.model_copy(
                            update={"latency_ms": int((time.monotonic() - started) * 1000)}
                        )
                    }
                )
                episode.tokens += result.audit.total_tokens - reserve_input - reserve_output
                episode.cost = max(0, episode.cost + result.audit.cost - reserve_cost)
            episode.hashes[work.request_id], episode.records[work.request_id] = (
                work.request_hash,
                result,
            )
            return result

    async def _dispatch(
        self, work: InferenceWork, reserve_input: int, reserve_output: int, reserve_cost: float
    ) -> InferenceResult:
        p = self.profile
        body: dict[str, Any] = {
            "model": p.model,
            "messages": [m.model_dump() for m in work.request.messages],
            "temperature": p.temperature,
            "top_p": p.top_p,
            p.token_parameter: p.max_output_tokens,
        }
        if p.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"X-Request-ID": work.request_id}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        try:
            async with (
                self._slots,
                self._client.stream(
                    "POST", "chat/completions", headers=headers, json=body
                ) as response,
            ):
                if not response.is_success:
                    raise ValueError("provider_rejected_request")
                chunks, received = [], 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > p.max_response_bytes:
                        raise ValueError("provider_response_limit")
                    chunks.append(chunk)
                raw = json.loads(b"".join(chunks))
                content = raw["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise ValueError("provider_invalid_response")
                usage = raw.get("usage") or {}
                input_tokens, output_tokens = (
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                )
                valid_usage = all(
                    isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 100_000_000
                    for n in (input_tokens, output_tokens)
                )
                if not valid_usage:
                    input_tokens, output_tokens = reserve_input, reserve_output
                input_tokens, output_tokens = cast(int, input_tokens), cast(int, output_tokens)
                cost = (
                    input_tokens * p.input_cost_per_million
                    + output_tokens * p.output_cost_per_million
                ) / 1_000_000

                def clean(value: Any) -> str | None:
                    if not isinstance(value, str):
                        return None
                    return value.replace(self._key, "[REDACTED]") if self._key else value

                return self._result(
                    work,
                    status="SUCCEEDED",
                    content=clean(content),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost=cost,
                    usage_source="provider" if valid_usage else "conservative_reservation",
                    resolved_model=(clean(raw.get("model")) or "")[:200] or None,
                    provider_request_id=(
                        clean(response.headers.get("x-request-id") or raw.get("id")) or ""
                    )[:200]
                    or None,
                    fingerprint=(clean(raw.get("system_fingerprint")) or "")[:200] or None,
                )
        except httpx.TimeoutException:
            return self._result(
                work,
                status="TIMED_OUT",
                code="provider_timeout",
                input_tokens=reserve_input,
                output_tokens=reserve_output,
                cost=reserve_cost,
                usage_source="conservative_reservation",
            )
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            # Provider errors can contain credentials, headers or prompts. Keep
            # them outside all target responses, exception strings and audit rows.
            return self._result(
                work,
                status="FAILED",
                code="provider_request_failed",
                input_tokens=reserve_input,
                output_tokens=reserve_output,
                cost=reserve_cost,
                usage_source="conservative_reservation",
            )
