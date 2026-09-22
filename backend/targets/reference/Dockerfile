FROM ghcr.io/astral-sh/uv:0.11.33 AS uv
FROM python:3.12.13-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
# Only the opaque target implementation enters this image. No scenario catalog,
# ground truth, verifier, attack fixtures, database or control-plane source.
COPY src/aml_reference_target ./src/aml_reference_target
COPY src/aml_target_protocol ./src/aml_target_protocol
USER 10001:10001
EXPOSE 8081
ENTRYPOINT ["python", "-m", "aml_reference_target"]
CMD ["--mode", "fixture"]
