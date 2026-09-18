.PHONY: setup test-setup check test test-all test-backend test-backend-live test-ui test-regression test-e2e

setup:
	uv sync --project backend --extra dev --locked
	uv pip install --python backend/.venv/bin/python --editable ./sdk
	npm ci --prefix ui
	test -f backend/.env || cp backend/.env.example backend/.env

test-setup: setup
	cd ui && npx playwright install chromium firefox webkit

# Fast local suite. Environment-backed gates explicitly report skips.
test: check test-backend test-ui

# Complete suite: provisions disposable Docker services and requires zero skips.
test-all: check test-backend-live test-ui

check:
	cd backend && uv run ruff check --config pyproject.toml . ../sdk ../ui/e2e/server.py
	cd backend && uv run pyright
	cd ui && npm run lint && npm run typecheck

test-backend:
	cd backend && OTEL_ENABLED=false uv run pytest

test-backend-live:
	cd backend && OTEL_ENABLED=false uv run python scripts/test-live.py

test-ui:
	cd ui && npm run test:all

test-regression:
	cd backend && OTEL_ENABLED=false uv run pytest tests/regression --no-cov
	cd ui && npm run build && npm run test:unit

test-e2e:
	cd backend && OTEL_ENABLED=false uv run pytest tests/end_to_end --no-cov
	cd ui && npm run test:e2e
