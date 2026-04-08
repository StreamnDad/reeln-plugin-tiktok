PLUGIN_PKG := reeln_tiktok_plugin

.PHONY: dev-install install test lint format check login

VENV := .venv/bin

dev-install:
	uv venv --clear
	uv pip install -e ../reeln-cli
	uv pip install -e ".[dev]"

install:
	uv pip install --python ~/.local/share/uv/tools/reeln/bin/python3 -e .

test:
	$(VENV)/python -m pytest tests/ -n auto --cov=$(PLUGIN_PKG) --cov-branch --cov-fail-under=100 -q

lint:
	$(VENV)/ruff check .

format:
	$(VENV)/ruff format .

check: lint
	$(VENV)/mypy $(PLUGIN_PKG)/
	$(MAKE) test

login:
	$(VENV)/python -m $(PLUGIN_PKG) \
		--client-key $(CLIENT_KEY) \
		--client-secret-file $(CLIENT_SECRET_FILE)
