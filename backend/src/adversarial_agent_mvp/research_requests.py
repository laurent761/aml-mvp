from __future__ import annotations

import json
import math
import time
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .settings import Settings


def finite_json_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def validate_json_body(body: bytes | bytearray) -> None:
    document = json.loads(body, parse_float=finite_json_number, parse_constant=finite_json_number)
    pending = [(document, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            # Leave room for the API's record wrappers in downstream serializers.
            if depth >= 128:
                raise ValueError("JSON nesting limit exceeded")
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)


class ResearchRequestMiddleware:
    """Validate API requests and enforce limits for the single-admin POC."""
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app, self.settings = app, settings
        self.requests: tuple[int, int] = (0, 0)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/v1/") or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        if headers.get(b"x-aml-api-version", b"aml.research.v1") != b"aml.research.v1":
            await JSONResponse({"detail": "unsupported research API version"}, status_code=406)(scope, receive, send)
            return
        path = scope["path"]
        minute = int(time.time() // 60)
        previous, count = self.requests
        count = count + 1 if previous == minute else 1
        self.requests = (minute, count)
        if count > self.settings.research_requests_per_minute:
            await JSONResponse({"detail": "request limit exceeded"}, status_code=429,
                               headers={"Retry-After": "60"})(scope, receive, send)
            return
        # Ordinary JSON is bounded before FastAPI parses it. Checkpoint data remains streamed.
        if scope["method"] in {"POST", "PUT"} and not (scope["method"] == "PUT" and path.startswith("/v1/artifact-uploads/")):
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.settings.research_max_request_bytes:
                    await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            if body:
                try:
                    validate_json_body(body)
                except (ValueError, RecursionError):
                    await JSONResponse({"detail": "valid finite JSON with at most 128 nesting levels required"}, status_code=422)(scope, receive, send)
                    return
            delivered = False
            async def replay() -> dict[str, Any]:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return dict(await receive())
            await self.app(scope, replay, send)
        else:
            await self.app(scope, receive, send)
