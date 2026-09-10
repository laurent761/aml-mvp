import time

import pytest

from adversarial_agent_mvp.security import CapabilityError, CapabilityTokenService, redact_sensitive


def service():
    return CapabilityTokenService("k" * 32)


def test_capability_is_episode_and_destination_scoped():
    token = service().issue("e1", ["payments"])
    assert service().verify(token, episode_id="e1", destination="payments")["episode_id"] == "e1"
    with pytest.raises(CapabilityError, match="destination"):
        service().verify(token, episode_id="e1", destination="mail")
    with pytest.raises(CapabilityError, match="episode"):
        service().verify(token, episode_id="e2", destination="payments")


def test_tampered_token_is_rejected():
    token = service().issue("e1", ["payments"])
    with pytest.raises(CapabilityError):
        service().verify(token[:-1] + "x", episode_id="e1", destination="payments")


def test_equivalent_noncanonical_signature_encoding_is_rejected():
    token = service().issue("e1", ["payments"])
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    alias = token[:-1] + alphabet[alphabet.index(token[-1]) + 1]
    with pytest.raises(CapabilityError, match="noncanonical"):
        service().verify(alias, episode_id="e1", destination="payments")
    with pytest.raises(CapabilityError):
        service().verify(token + "=", episode_id="e1", destination="payments")


def test_expired_token_is_rejected(monkeypatch):
    token = service().issue("e1", ["payments"], ttl_seconds=1)
    monkeypatch.setattr(time, "time", lambda: 10**12)
    with pytest.raises(CapabilityError, match="expired"):
        service().verify(token, episode_id="e1", destination="payments")


def test_error_redaction():
    assert "super-secret" not in redact_sensitive("failed super-secret", ["super-secret"])
