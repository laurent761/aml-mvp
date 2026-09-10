import pytest
from pydantic import ValidationError

from adversarial_agent_mvp.red_contracts import ModelProvider
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.worker import effective_model_config


def test_production_rejects_development_secrets() -> None:
    with pytest.raises(ValidationError, match="development credentials"):
        Settings(
            deployment_environment="production",
            database_url="postgresql+psycopg://user:password@db/database",
        )


def test_firecracker_requires_external_runner_configuration() -> None:
    with pytest.raises(ValidationError, match="FIRECRACKER_RUNNER_URL"):
        Settings(capsule_runtime="firecracker")


def test_empty_optional_environment_values_are_unset() -> None:
    settings = Settings(
        firecracker_runner_url="",
        firecracker_runner_token="",
        attacker_model_api_key="",
    )
    assert settings.firecracker_runner_url is None
    assert settings.firecracker_runner_token is None
    assert settings.attacker_model_api_key is None


def test_service_role_does_not_require_unrelated_production_secrets() -> None:
    api = Settings(
        deployment_environment="production",
        service_role="api",
        database_url="postgresql+psycopg://user:password@db/database",
        research_auth_required=True,
        research_auth_tokens={"a" * 64: {"owner_id": "operator", "scopes": ["operator"]}},
    )
    blue = Settings(
        deployment_environment="production",
        service_role="blue-gateway",
        capability_signing_key="production-signing-key-that-is-long-enough",
    )

    assert api.service_role == "api"
    assert blue.service_role == "blue-gateway"


def test_local_openai_compatible_model_is_runnable_without_api_key() -> None:
    settings = Settings(
        attacker_model_provider="local_openai_compatible",
        attacker_model_base_url="http://model.internal/v1",
        attacker_model_name="local-red",
    )

    config = effective_model_config(settings)

    assert config.provider == ModelProvider.LOCAL_OPENAI_COMPATIBLE
    assert config.model == "local-red"


def test_hosted_openai_compatible_model_requires_api_key() -> None:
    with pytest.raises(ValidationError, match="ATTACKER_MODEL_API_KEY"):
        Settings(
            attacker_model_provider="hosted_openai_compatible",
            attacker_model_base_url="https://models.example/v1",
            attacker_model_name="hosted-red",
        )
