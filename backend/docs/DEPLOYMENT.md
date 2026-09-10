# Deployment Guide

This guide describes the single-node Docker Compose profile used for internal research and design-partner evaluation. It packages the operator UI, control API, campaign worker, capsule supervisor, Blue gateway image, PostgreSQL, MinIO, MLflow, and an OpenTelemetry collector.

The repository includes a synthetic finance reference target for fixture validation.
Build and register it with the [root run guide](../../README.md). Real model selection
remains a researcher configuration step. Register external OCI targets only after
validating their interface manifests and image digests.

## Prerequisites

- Linux or Docker Desktop with Docker Engine and Compose v2
- Docker access for the operator starting the stack
- At least 4 CPU cores, 8 GiB RAM, and 20 GiB free storage for a small evaluation
- A target OCI image available to the Docker daemon

The capsule supervisor is the only application service that receives the Docker socket. Treat it as a privileged control-plane component even though its container runs as a non-root user with dropped Linux capabilities.

## Configure

```bash
cp .env.example .env
```

The checked-in values are development-only. For any non-development deployment, replace at least:

- `POSTGRES_PASSWORD`
- `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD`
- `CAPABILITY_SIGNING_KEY`
- `CAPSULE_SUPERVISOR_TOKEN`

Generate signing material with a cryptographically secure generator. Both application tokens must be at least 32 characters. The process refuses the checked-in development signing values when `DEPLOYMENT_ENVIRONMENT` is `test` or `production`; production also refuses SQLite.

Compose assigns a fixed `SERVICE_ROLE` to each process. Settings validation uses that
role so each container receives only the secrets it actually consumes: the API gets
database/object-store credentials, the worker additionally gets the model and
supervisor credentials, Blue gets only its signing key, and the Docker-privileged
supervisor gets only its lifecycle/signing credentials. Do not override these roles or
reintroduce a shared all-secrets environment block when adapting this profile.

On Linux, set `DOCKER_GID` to the group owner of the Docker socket:

```bash
stat -c '%g' /var/run/docker.sock
```

Keep `BIND_ADDRESS=127.0.0.1` unless ingress authentication and TLS are supplied outside this repository.

## Build and start

```bash
docker compose build --pull
docker compose up -d
docker compose ps
```

The image build uses `uv sync --frozen` against `uv.lock`. A dependency change therefore fails the build until the lockfile is intentionally regenerated.

Startup ordering is health-gated:

1. PostgreSQL and MinIO become healthy.
2. The MinIO initialization job creates the evidence and MLflow buckets.
3. Alembic upgrades PostgreSQL.
4. MLflow, the Blue gateway image, and the capsule supervisor become healthy.
5. The control API and worker start.
6. The operator UI starts after the API is healthy.

Host endpoints default to loopback:

| Service | URL |
|---|---|
| Operator console | `http://127.0.0.1:3000` |
| Control API | `http://127.0.0.1:8000` |
| OpenAPI | `http://127.0.0.1:8000/docs` |
| MinIO API | `http://127.0.0.1:9000` |
| MinIO console | `http://127.0.0.1:9001` |
| MLflow | `http://127.0.0.1:5000` |
| OTLP gRPC | `127.0.0.1:4317` |
| OTLP HTTP | `http://127.0.0.1:4318` |

The supervisor and Blue gateway lifecycle APIs are intentionally not published to the host.

The UI uses a same-origin Worker proxy with a strict route and header allowlist. In Compose, `BACKEND_API_URL=http://api:8000` is server-side configuration; backend credentials and the Docker control plane are never exposed to the browser. The UI build context defaults to `../ui`, matching the packaged platform layout.

## Capsule supervisor contract

The worker-side adapter should use `CapsuleSupervisorClient` from `adversarial_agent_mvp.supervisor`. It implements the existing `CapsuleRuntime` methods over authenticated HTTP:

| Operation | Endpoint |
|---|---|
| Create | `POST /v1/capsules` with a `CapsuleSpec` |
| Health | `GET /v1/capsules/{capsule_id}/health` |
| Firecracker-compatible readiness alias | `GET /v1/capsules/{capsule_id}/ready` |
| Reset | `POST /v1/capsules/{capsule_id}/reset` |
| Destroy | `DELETE /v1/capsules/{capsule_id}` |
| Trusted Blue/target proxy | `/v1/capsules/{capsule_id}/gateway/{path}` |

All lifecycle and proxy endpoints require `Authorization: Bearer <CAPSULE_SUPERVISOR_TOKEN>`. Only `/healthz` is unauthenticated. The supervisor bounds active capsules with `MAX_CAPSULE_CONCURRENCY` and attempts teardown during graceful shutdown. Docker containers and internal networks carry managed, capsule, episode, and role ownership labels. Before accepting traffic after a restart, the supervisor inventories those labels and removes every resource not owned by an active in-memory capsule; repeated reconciliation is idempotent. A non-missing cleanup failure aborts startup instead of silently leaking a capsule. The health response reports sanitized reconciliation counts.

The server response never contains Blue's internal supervisor capability; that credential remains inside the supervisor process. It does include a sanitized runtime containment proof after Docker has verified one internal network, exactly the expected Blue and target members, and all ownership labels. The worker persists that proof as an immutable episode event, and the episode API and evidence bundle expose it without raw container or network identifiers. `CapsuleSupervisorClient` annotates its local handle with the already configured supervisor bearer token so the trusted target adapter can authenticate to the proxy.

The Docker implementation launches the configured `BLUE_GATEWAY_IMAGE`, which Compose builds as `blue-gateway:local`. Target invocation and health endpoints must resolve through the capsule-local trusted adapter; never configure the trusted worker to call customer-provided absolute URLs directly.

## Firecracker runner interface

Firecracker/KVM is not bundled or claimed validated by this repository. For a customer-pilot isolation profile, deploy a separately audited runner that implements the same authenticated supervisor contract, then configure:

```text
CAPSULE_RUNTIME=firecracker
FIRECRACKER_RUNNER_URL=https://runner.internal.example
FIRECRACKER_RUNNER_TOKEN=<independent random token>
```

The local capsule supervisor becomes a narrow proxy to that runner. Do not set `CAPSULE_RUNTIME=firecracker` until the external runner has passed containment tests for network egress, metadata access, host access, resource exhaustion, teardown, and concurrent tenants.

## Persistence

- PostgreSQL operational data: `postgres-data`
- MinIO evidence and MLflow artifacts: `minio-data`
- MLflow run metadata: `mlflow-data`
- Local artifact fallback: `local-artifacts`

The application artifact backend is selected with `ARTIFACT_BACKEND`. The Compose profile selects `s3`, points it at MinIO, and initializes `S3_BUCKET`. Local storage is intended only for development.

Back up PostgreSQL and MinIO together so database artifact references and objects remain consistent. Test restoration before a design-partner campaign. MLflow metadata can be backed up separately, but it should be retained with the campaign evidence it describes.

## Telemetry

Application telemetry is configured by:

```text
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_EXPORT_INTERVAL_SECONDS=15
```

`configure_telemetry()` emits JSON logs and configures OTLP traces and metrics. Entrypoints must call it once, instrument FastAPI applications with `instrument_fastapi()`, and instrument SQLAlchemy engines with `instrument_sqlalchemy()`. The included collector exports to its own debug log for local inspection; replace that exporter with a customer-approved durable backend before production use.

MLflow is persisted and uses MinIO for artifacts. Set `MLFLOW_TRACKING_URI` and
`MLFLOW_EXPERIMENT_NAME` to enable the optional adapters. The worker-facing
`build_mlflow_campaign_tracker()` adapter resolves or creates the experiment once,
runs its synchronous REST calls off the event loop, records a canonical configuration
SHA-256 plus finite numeric campaign metrics, and explicitly closes every started run
as `FINISHED` or `FAILED`. `build_mlflow_evaluation_hook()` supplies the same resolved
experiment to the offline evaluation pipeline. Both factories return `None` when the
tracking URI is unset and raise `MlflowTrackingError` with a credential-safe message
when configured MLflow cannot be initialized or written.

The application logs only bounded parameters, references, metrics, and hashes of
secret-like values; raw campaign configuration, prompts, and credentials must not be
sent to MLflow. MLflow export failures remain explicit: a configured but unavailable
tracking server is not silently treated as successful experiment recording.

## Upgrade and rollback

Before upgrading:

1. Back up PostgreSQL, MinIO, and MLflow volumes.
2. Build the new locked image.
3. Inspect the pending Alembic revision.
4. Run `docker compose run --rm migration` in a staging copy.
5. Run the containment and end-to-end validation profiles.

Apply an upgrade with:

```bash
docker compose run --rm migration
docker compose up -d --remove-orphans
```

Rollback is artifact-specific. Restore the previous application image and database backup unless the corresponding Alembic downgrade has been explicitly tested against customer data.

## Verification checklist

Do not call a deployment validated solely because unit tests pass. A release environment must demonstrate:

- PostgreSQL migration from an empty database
- MinIO write, read, integrity verification, restart, and restore
- MLflow run creation and artifact persistence across restart
- OTLP trace and metric receipt
- Real OCI target startup and health check
- Target reachability only to Blue
- Failed DNS, public internet, RFC1918 production ranges, and cloud metadata access
- Successful teardown after completion, failure, cancellation, and supervisor restart
- Finding replay with a versioned policy and benign regression behavior

## Stop

```bash
docker compose down
```

This preserves named volumes. `docker compose down --volumes` permanently deletes local PostgreSQL, MinIO, MLflow, and fallback artifact data and should be used only for an intentionally disposable environment.
