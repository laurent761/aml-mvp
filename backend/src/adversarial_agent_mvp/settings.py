from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_CAPABILITY_SIGNING_KEY = "development-only-signing-key-change-me"
DEVELOPMENT_SUPERVISOR_TOKEN = "development-only-supervisor-token-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    deployment_environment: Literal["development", "test", "production"] = "development"
    service_role: Literal[
        "all",
        "api",
        "worker",
        "capsule-supervisor",
        "blue-gateway",
    ] = "all"
    service_name: str = "adversarial-agent-mvp"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: str = "sqlite:///./adversarial_mvp.db"
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:4173",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:4173",
            "http://127.0.0.1:5173",
            "http://terminal.local:4173",
        ]
    )

    artifact_backend: Literal["local", "s3"] = "local"
    artifact_root: Path = Path("./var/artifacts")
    s3_bucket: str = "adversarial-evidence"
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"

    capsule_runtime: Literal["docker", "firecracker"] = "docker"
    capsule_supervisor_url: str | None = None
    capsule_supervisor_token: str = Field(
        default=DEVELOPMENT_SUPERVISOR_TOKEN,
        min_length=32,
    )
    capsule_supervisor_host: str = "0.0.0.0"
    capsule_supervisor_port: int = Field(default=8090, ge=1, le=65535)
    max_capsule_concurrency: int = Field(default=4, ge=1, le=64)
    blue_gateway_image: str = "blue-gateway:local"
    firecracker_runner_url: str | None = None
    firecracker_runner_token: str | None = Field(default=None, min_length=32)

    capability_signing_key: str = Field(
        default=DEVELOPMENT_CAPABILITY_SIGNING_KEY,
        min_length=32,
    )
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 30
    worker_max_attempts: int = Field(default=3, ge=1, le=20)
    worker_retry_base_seconds: float = Field(default=1.0, ge=0, le=300)
    worker_retry_max_seconds: float = Field(default=30.0, ge=0, le=3600)
    max_worker_concurrency: int = 4

    research_max_sessions_per_owner: int = Field(default=4, ge=1, le=64)
    research_owner_cost_limit: float = Field(default=100, ge=0, allow_inf_nan=False)
    research_owner_storage_bytes: int = Field(default=107374182400, ge=1)
    research_max_request_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    research_requests_per_minute: int = Field(default=600, ge=1)
    research_upload_root: Path = Path("./var/uploads")
    research_upload_expiry_seconds: int = Field(default=86400, ge=60)

    attacker_model_provider: Literal[
        "heuristic",
        "hosted_openai_compatible",
        "local_openai_compatible",
        "openai-compatible",
    ] = "heuristic"
    attacker_model_base_url: str | None = None
    attacker_model_name: str | None = None
    attacker_model_api_key: str | None = None
    attacker_model_input_cost_per_million: float = Field(default=0, ge=0)
    attacker_model_output_cost_per_million: float = Field(default=0, ge=0)

    target_model_provider: Literal[
        "disabled", "local_openai_compatible", "hosted_openai_compatible"
    ] = "disabled"
    target_model_base_url: str | None = None
    target_model_name: str | None = None
    target_model_api_key: SecretStr | None = None
    target_model_revision: str | None = None
    target_model_temperature: float = Field(default=0, ge=0, le=2, allow_inf_nan=False)
    target_model_top_p: float = Field(default=1, gt=0, le=1, allow_inf_nan=False)
    target_model_max_output_tokens: int = Field(default=1000, ge=1, le=32768)
    target_model_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    target_model_json_mode: bool = True
    target_model_timeout_seconds: int = Field(default=30, ge=1, le=120)
    target_model_max_input_bytes: int = Field(default=65536, ge=256, le=262144)
    target_model_max_response_bytes: int = Field(default=262144, ge=1024, le=1048576)
    target_model_max_requests: int = Field(default=100, ge=1, le=1000)
    target_model_max_total_tokens: int = Field(default=200000, ge=1, le=10000000)
    target_model_max_cost: float = Field(default=10, ge=0, le=1000, allow_inf_nan=False)
    target_model_input_cost_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    target_model_output_cost_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    target_model_max_concurrency: int = Field(default=4, ge=1, le=64)

    mlflow_tracking_uri: str | None = None
    mlflow_experiment_name: str = "adversarial-agent-mvp"

    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str | None = None
    otel_export_interval_seconds: int = Field(default=15, ge=1, le=300)

    @field_validator(
        "s3_endpoint_url",
        "capsule_supervisor_url",
        "firecracker_runner_url",
        "firecracker_runner_token",
        "attacker_model_base_url",
        "attacker_model_name",
        "attacker_model_api_key",
        "target_model_base_url",
        "target_model_name",
        "target_model_api_key",
        "target_model_revision",
        "mlflow_tracking_uri",
        "otel_exporter_otlp_endpoint",
        mode="before",
    )
    @classmethod
    def empty_string_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_deployment_safety(self) -> "Settings":
        if self.worker_retry_max_seconds < self.worker_retry_base_seconds:
            raise ValueError("WORKER_RETRY_MAX_SECONDS must be >= WORKER_RETRY_BASE_SECONDS")
        if self.deployment_environment != "development":
            unsafe: list[str] = []
            if self.service_role in {"all", "capsule-supervisor", "blue-gateway"} and (
                self.capability_signing_key == DEVELOPMENT_CAPABILITY_SIGNING_KEY
            ):
                unsafe.append("CAPABILITY_SIGNING_KEY")
            if self.service_role in {"all", "worker", "capsule-supervisor"} and (
                self.capsule_supervisor_token == DEVELOPMENT_SUPERVISOR_TOKEN
            ):
                unsafe.append("CAPSULE_SUPERVISOR_TOKEN")
            if unsafe:
                raise ValueError(
                    "development credentials are forbidden outside development: "
                    + ", ".join(unsafe)
                )

        if (
            self.deployment_environment == "production"
            and self.service_role in {"all", "api", "worker"}
            and self.database_url.startswith("sqlite")
        ):
            raise ValueError("production requires PostgreSQL; SQLite is development-only")

        if self.service_role in {"all", "capsule-supervisor"} and (
            self.capsule_runtime == "firecracker"
        ):
            if not self.firecracker_runner_url:
                raise ValueError(
                    "FIRECRACKER_RUNNER_URL is required when CAPSULE_RUNTIME=firecracker"
                )
            if not self.firecracker_runner_token:
                raise ValueError(
                    "FIRECRACKER_RUNNER_TOKEN is required when CAPSULE_RUNTIME=firecracker"
                )

        if self.service_role in {"all", "worker"} and self.attacker_model_provider != "heuristic":
            missing = [
                name
                for name, value in (
                    ("ATTACKER_MODEL_BASE_URL", self.attacker_model_base_url),
                    ("ATTACKER_MODEL_NAME", self.attacker_model_name),
                )
                if not value
            ]
            if (
                self.attacker_model_provider in {"hosted_openai_compatible", "openai-compatible"}
                and not self.attacker_model_api_key
            ):
                missing.append("ATTACKER_MODEL_API_KEY")
            if missing:
                raise ValueError(
                    "OpenAI-compatible attacker model configuration is incomplete: "
                    + ", ".join(missing)
                )

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
