.PHONY: setup

setup:
	uv sync --project backend --extra dev --locked
	npm ci --prefix ui
	test -f backend/.env || cp backend/.env.example backend/.env
