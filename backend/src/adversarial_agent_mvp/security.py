from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


class CapabilityError(ValueError):
    pass


class CapabilityTokenService:
    def __init__(self, key: str):
        if len(key) < 32:
            raise ValueError("capability signing key must be at least 32 characters")
        self._key = key.encode()

    def issue(self, episode_id: str, destinations: list[str], ttl_seconds: int = 300) -> str:
        payload = {
            "episode_id": episode_id,
            "destinations": sorted(set(destinations)),
            "exp": int(time.time()) + ttl_seconds,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._key, raw, hashlib.sha256).digest()
        return f"{self._encode(raw)}.{self._encode(signature)}"

    def verify(self, token: str, *, episode_id: str, destination: str) -> dict[str, Any]:
        try:
            raw_text, sig_text = token.split(".", 1)
            raw, signature = self._decode(raw_text), self._decode(sig_text)
            expected = hmac.new(self._key, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise CapabilityError("invalid capability signature")
            payload = json.loads(raw)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            if isinstance(exc, CapabilityError):
                raise
            raise CapabilityError("malformed capability token") from exc
        if int(payload.get("exp", 0)) < int(time.time()):
            raise CapabilityError("capability token expired")
        if payload.get("episode_id") != episode_id:
            raise CapabilityError("capability token episode mismatch")
        if destination not in payload.get("destinations", []):
            raise CapabilityError("destination not authorized")
        return payload

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    @staticmethod
    def _decode(value: str) -> bytes:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        # Reject alternate padding-bit encodings of the same signed bytes.
        if CapabilityTokenService._encode(decoded) != value:
            raise CapabilityError("noncanonical capability encoding")
        return decoded


def redact_sensitive(text: str, values: list[str]) -> str:
    result = text
    for value in sorted((v for v in values if v), key=len, reverse=True):
        result = result.replace(value, "[REDACTED]")
    return result
