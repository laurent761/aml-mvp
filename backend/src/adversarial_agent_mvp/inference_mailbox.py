"""Bounded, episode-scoped mailbox. Blue has no provider route or credentials."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from .inference_contracts import (
    InferenceAudit,
    InferenceResult,
    InferenceWork,
    TargetInferenceProfile,
    TargetInferenceRequest,
    inference_hash,
)


@dataclass
class PendingInference:
    work: InferenceWork
    future: asyncio.Future[InferenceResult]
    claimed: bool = False


class InferenceMailbox:
    def __init__(self, episode_id: str, profile: TargetInferenceProfile):
        self.episode_id, self.profile = episode_id, profile
        self.requests: dict[str, PendingInference] = {}
        self.records: list[dict] = []
        self.closed = False

    async def submit(self, request_id: str, request: TargetInferenceRequest) -> InferenceResult:
        if self.closed:
            raise ValueError("inference episode is closed")
        if request.model not in {None, self.profile.model}:
            raise ValueError("model is outside the inference profile")
        encoded = json.dumps(
            [m.model_dump() for m in request.messages], ensure_ascii=False
        ).encode()
        if len(encoded) > self.profile.max_input_bytes:
            raise ValueError("inference input limit exceeded")
        work = InferenceWork(
            request_id=request_id,
            episode_id=self.episode_id,
            profile_sha256=self.profile.sha256,
            request_hash=inference_hash(request.model_dump(mode="json")),
            request=request,
        )
        pending = self.requests.get(request_id)
        if pending:
            if pending.work.request_hash != work.request_hash:
                raise ValueError("inference correlation ID was reused with different input")
        else:
            if len(self.requests) >= self.profile.max_requests:
                raise OverflowError("inference request budget exhausted")
            if sum(not p.future.done() for p in self.requests.values()) >= 4:
                raise OverflowError("inference queue is full")
            pending = PendingInference(work, asyncio.get_running_loop().create_future())
            self.requests[request_id] = pending
        # Timeout does not cancel an already dispatched provider call or permit a
        # duplicate call. Its eventual audit result remains available privately.
        try:
            return await asyncio.wait_for(
                asyncio.shield(pending.future), self.profile.timeout_seconds + 15
            )
        except TimeoutError:
            if not pending.claimed and not pending.future.done():
                self.fail(pending, "relay_queue_timeout")
            raise

    def fail(self, pending: PendingInference, code: str) -> None:
        if pending.future.done():
            return
        p = self.profile
        request = pending.work.request
        size = len(
            json.dumps([m.model_dump() for m in request.messages], ensure_ascii=False).encode()
        )
        input_tokens = size + 64 * len(request.messages) + 256 if pending.claimed else 0
        output_tokens = p.max_output_tokens if pending.claimed else 0
        self.complete(
            InferenceResult(
                request_id=pending.work.request_id,
                audit=InferenceAudit(
                    request_id=pending.work.request_id,
                    episode_id=self.episode_id,
                    request_hash=pending.work.request_hash,
                    profile=p,
                    status="FAILED",
                    error_code=code,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    usage_source="conservative_reservation"
                    if pending.claimed
                    else "not_dispatched",
                    cost=(
                        input_tokens * p.input_cost_per_million
                        + output_tokens * p.output_cost_per_million
                    )
                    / 1_000_000,
                    cost_source="operator_rates"
                    if p.input_cost_per_million or p.output_cost_per_million
                    else "unpriced",
                    reproducibility="operator_revision_declared"
                    if p.declared_revision
                    else "provider_revision_unavailable",
                ),
            )
        )

    def fail_pending(self) -> None:
        self.closed = True
        for pending in self.requests.values():
            self.fail(pending, "inference_relay_failed")

    def claim(self) -> list[InferenceWork]:
        if self.closed:
            return []
        for pending in self.requests.values():
            if not pending.claimed and not pending.future.done():
                pending.claimed = True
                return [pending.work]
        return []

    def complete(self, result: InferenceResult) -> None:
        pending = self.requests.get(result.request_id)
        if pending is None:
            raise ValueError("inference request is unknown")
        if (
            result.audit.episode_id != self.episode_id
            or result.audit.request_id != result.request_id
            or result.audit.request_hash != pending.work.request_hash
            or result.audit.profile != self.profile
        ):
            raise ValueError("inference completion does not match its request")
        if pending.future.done():
            if pending.future.cancelled() or pending.future.result() != result:
                raise ValueError("inference completion conflicts with its recorded result")
            return
        self.records.append(
            {"sequence": len(self.records) + 1, **result.audit.model_dump(mode="json")}
        )
        pending.future.set_result(result)

    def close(self) -> None:
        self.closed = True
        for pending in self.requests.values():
            if not pending.future.done():
                pending.future.cancel()
