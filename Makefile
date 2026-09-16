.PHONY: setup

setup:
	uv sync --project backend --extra dev --locked
	uv pip install --python backend/.venv/bin/python --editable ./sdk
	npm ci --prefix ui
	test -f backend/.env || cp backend/.env.example backend/.env
