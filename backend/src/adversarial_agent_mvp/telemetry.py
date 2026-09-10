from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy import Engine

    from .settings import Settings


_runtime: TelemetryRuntime | None = None


class JsonLogFormatter(logging.Formatter):
    """Compact structured logs without serializing request bodies or credentials."""

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            document["trace_id"] = format(span_context.trace_id, "032x")
            document["span_id"] = format(span_context.span_id, "016x")
        if record.exc_info:
            document["exception"] = self.formatException(record.exc_info)
        return json.dumps(document, separators=(",", ":"), ensure_ascii=False)


def configure_structured_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers[:] = [handler]
        logger.propagate = False
        logger.setLevel(level)


@dataclass(slots=True)
class TelemetryRuntime:
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None

    def shutdown(self) -> None:
        if self.meter_provider is not None:
            self.meter_provider.shutdown()
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()


def configure_telemetry(
    settings: Settings,
    *,
    service_name: str | None = None,
) -> TelemetryRuntime:
    """Configure JSON logging and optional OTLP traces/metrics once per process."""

    global _runtime
    configure_structured_logging(settings.log_level)
    if _runtime is not None:
        return _runtime
    if not settings.otel_enabled:
        _runtime = TelemetryRuntime()
        return _runtime

    if not settings.otel_exporter_otlp_endpoint:
        raise ValueError("OTEL_EXPORTER_OTLP_ENDPOINT is required when OTEL_ENABLED=true")

    resource = Resource.create(
        {
            "service.name": service_name or settings.service_name,
            "deployment.environment.name": settings.deployment_environment,
            "service.version": "0.1.0",
        }
    )
    endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
        export_interval_millis=settings.otel_export_interval_seconds * 1000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
    _runtime = TelemetryRuntime(tracer_provider, meter_provider)
    return _runtime


def instrument_fastapi(app: FastAPI) -> None:
    FastAPIInstrumentor.instrument_app(app, excluded_urls="/healthz,/readyz")


def instrument_sqlalchemy(engine: Engine) -> None:
    SQLAlchemyInstrumentor().instrument(engine=engine)


class MlflowTracker:
    """Small MLflow REST adapter that logs references and metrics, never credentials or prompts."""

    def __init__(self, tracking_uri: str, client: httpx.Client | None = None):
        self.tracking_uri = tracking_uri.rstrip("/")
        self.client = client or httpx.Client(timeout=10, trust_env=False)
        self._owns_client = client is None

    def create_run(
        self,
        experiment_id: str,
        run_name: str,
        tags: dict[str, str] | None = None,
    ) -> str:
        safe_tags = [{"key": key, "value": value} for key, value in (tags or {}).items()]
        safe_tags.append({"key": "mlflow.runName", "value": run_name})
        response = self.client.post(
            f"{self.tracking_uri}/api/2.0/mlflow/runs/create",
            json={"experiment_id": experiment_id, "tags": safe_tags},
        )
        response.raise_for_status()
        return str(response.json()["run"]["info"]["run_id"])

    def get_or_create_experiment(self, name: str) -> str:
        response = self.client.get(
            f"{self.tracking_uri}/api/2.0/mlflow/experiments/get-by-name",
            params={"experiment_name": name},
        )
        if response.is_success:
            return str(response.json()["experiment"]["experiment_id"])
        if response.status_code != 404:
            response.raise_for_status()

        created = self.client.post(
            f"{self.tracking_uri}/api/2.0/mlflow/experiments/create",
            json={"name": name},
        )
        if created.status_code == 409:
            # Another worker won the create race; resolve the now-existing experiment.
            response = self.client.get(
                f"{self.tracking_uri}/api/2.0/mlflow/experiments/get-by-name",
                params={"experiment_name": name},
            )
            response.raise_for_status()
            return str(response.json()["experiment"]["experiment_id"])
        created.raise_for_status()
        return str(created.json()["experiment_id"])

    def log_batch(
        self,
        run_id: str,
        *,
        metrics: dict[str, float] | None = None,
        params: dict[str, Any] | None = None,
        step: int = 0,
    ) -> None:
        safe_params = []
        for key, value in (params or {}).items():
            rendered = str(value)
            if any(
                label in key.lower()
                for label in (
                    "authorization",
                    "credential",
                    "password",
                    "secret",
                    "token",
                    "key",
                )
            ):
                rendered = "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()
            safe_params.append({"key": key, "value": rendered[:6000]})
        timestamp = int(time.time() * 1000)
        response = self.client.post(
            f"{self.tracking_uri}/api/2.0/mlflow/runs/log-batch",
            json={
                "run_id": run_id,
                "metrics": [
                    {
                        "key": key,
                        "value": value,
                        "timestamp": timestamp,
                        "step": step,
                    }
                    for key, value in (metrics or {}).items()
                ],
                "params": safe_params,
                "tags": [],
            },
        )
        response.raise_for_status()

    def terminate_run(self, run_id: str, status: str = "FINISHED") -> None:
        if status not in {"FINISHED", "FAILED", "KILLED"}:
            raise ValueError("invalid terminal MLflow run status")
        response = self.client.post(
            f"{self.tracking_uri}/api/2.0/mlflow/runs/update",
            json={
                "run_id": run_id,
                "status": status,
                "end_time": int(time.time() * 1000),
            },
        )
        response.raise_for_status()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class MlflowTrackingError(RuntimeError):
    """Stable error surface that does not expose request headers, prompts, or credentials."""


@dataclass(slots=True)
class MlflowCampaignTracker:
    """Async-safe campaign recorder around the synchronous MLflow REST client."""

    tracker: MlflowTracker
    experiment_name: str
    _experiment_id: str | None = field(default=None, init=False)
    _experiment_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def _resolve_experiment(self) -> str:
        if self._experiment_id is not None:
            return self._experiment_id
        async with self._experiment_lock:
            if self._experiment_id is None:
                try:
                    self._experiment_id = await asyncio.to_thread(
                        self.tracker.get_or_create_experiment,
                        self.experiment_name,
                    )
                except Exception:
                    raise MlflowTrackingError(
                        "MLflow experiment resolution failed"
                    ) from None
        experiment_id = self._experiment_id
        if experiment_id is None:  # pragma: no cover - guarded by the lock above
            raise MlflowTrackingError("MLflow experiment resolution returned no id")
        return experiment_id

    async def record_campaign(
        self,
        *,
        campaign_id: str,
        config: dict[str, Any],
        metrics: dict[str, Any],
    ) -> None:
        canonical_config = json.dumps(
            config,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        config_hash = hashlib.sha256(canonical_config).hexdigest()
        safe_metrics = {
            key: float(value)
            for key, value in metrics.items()
            if isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        }
        experiment_id = await self._resolve_experiment()
        run_id: str | None = None
        try:
            run_id = await asyncio.to_thread(
                self.tracker.create_run,
                experiment_id,
                f"campaign-{campaign_id}",
                {
                    "campaign_id": campaign_id,
                    "config_sha256": config_hash,
                },
            )
            await asyncio.to_thread(
                self.tracker.log_batch,
                run_id,
                metrics=safe_metrics,
                params={
                    "config_sha256": config_hash,
                    "config_schema": str(config.get("schema_version", "unknown")),
                },
            )
            await asyncio.to_thread(self.tracker.terminate_run, run_id, "FINISHED")
        except Exception:
            if run_id is not None:
                try:
                    await asyncio.to_thread(self.tracker.terminate_run, run_id, "FAILED")
                except Exception:
                    pass
            raise MlflowTrackingError("MLflow campaign recording failed") from None

    async def aclose(self) -> None:
        await asyncio.to_thread(self.tracker.close)


def build_mlflow_campaign_tracker(
    settings: Settings,
    *,
    tracker: MlflowTracker | None = None,
) -> MlflowCampaignTracker | None:
    if not settings.mlflow_tracking_uri:
        return None
    return MlflowCampaignTracker(
        tracker or MlflowTracker(settings.mlflow_tracking_uri),
        settings.mlflow_experiment_name,
    )


def build_mlflow_evaluation_hook(
    settings: Settings,
    *,
    tracker: MlflowTracker | None = None,
) -> Any | None:
    """Create the optional synchronous evaluation hook only when MLflow is configured."""

    if not settings.mlflow_tracking_uri:
        return None
    owns_tracker = tracker is None
    selected_tracker = tracker or MlflowTracker(settings.mlflow_tracking_uri)
    try:
        experiment_id = selected_tracker.get_or_create_experiment(
            settings.mlflow_experiment_name
        )
    except Exception:
        if owns_tracker:
            selected_tracker.close()
        raise MlflowTrackingError(
            "MLflow evaluation hook initialization failed"
        ) from None
    from .evaluation import MlflowEvaluationHook

    return MlflowEvaluationHook(selected_tracker, experiment_id)
