FROM ghcr.io/astral-sh/uv:0.11.33 AS uv
FROM docker:27.5.1-cli AS docker-cli

FROM python:3.12.13-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_HTTP_TIMEOUT=300 \
    UV_HTTP_RETRIES=5 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app
COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable \
    && useradd --system --uid 10001 --create-home app \
    && mkdir -p /app/var/artifacts /app/var/uploads \
    && chown -R app:app /app

USER app

FROM runtime AS worker
CMD ["adversarial-worker"]

FROM runtime AS blue-gateway
CMD ["adversarial-blue-gateway"]

FROM runtime AS capsule-supervisor
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
CMD ["adversarial-capsule-supervisor"]

FROM runtime AS mlflow
USER root
RUN mkdir -p /mlflow && chown app:app /mlflow
USER app
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=10001,gid=10001 \
    uv sync --frozen --no-dev --group mlflow-server --no-editable
CMD ["mlflow", "server", "--host", "0.0.0.0", "--port", "5000"]

FROM runtime AS api
CMD ["adversarial-api"]
