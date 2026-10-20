PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
BOOTSTRAP_PYTHON ?= python3.11

.DEFAULT_GOAL := help

.PHONY: help install hooks lint fmt fmt-check layers test test-commits smoke check build clean \
        chrome chrome-stop accounts

help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## create .venv (3.11) and install dev dependencies
	$(BOOTSTRAP_PYTHON) -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

hooks: ## install the commit-msg hook and the commit template
	git config commit.template .gitmessage
	@mkdir -p .git/hooks
	@printf '#!/bin/sh\nexec "$(CURDIR)/.venv/bin/python" "$(CURDIR)/tools/commit_msg.py" "$$1"\n' \
		> .git/hooks/commit-msg
	@chmod +x .git/hooks/commit-msg
	@echo "installed .git/hooks/commit-msg and commit.template=.gitmessage"

lint: ## ruff lint
	$(PY) -m ruff check src tools tests

fmt: ## ruff format (rewrites files)
	$(PY) -m ruff format src tools tests

fmt-check: ## ruff format --check (what CI enforces)
	$(PY) -m ruff format --check src tools tests

layers: ## verify the layered import rules
	$(PY) tools/check_layers.py

test: ## run the test suite (offline)
	$(PY) -m pytest -q

test-commits: ## validate commit messages on the current branch
	@base=$$(git rev-parse --abbrev-ref @{u} 2>/dev/null | sed 's|.*/||'); \
	if git rev-parse --verify --quiet "origin/main" >/dev/null; then \
		$(PY) tools/commit_msg.py --range origin/main..HEAD; \
	else \
		$(PY) tools/commit_msg.py --range "HEAD~1..HEAD"; \
	fi

smoke: ## MCP stdio handshake + tools/list
	$(PY) tools/smoke_mcp.py

check: lint fmt-check layers test smoke ## everything CI runs (commit messages excluded)

build: ## build sdist + wheel
	$(PY) -m build

chrome: ## launch Chrome for an account: make chrome [ACCOUNT=work]
	bash tools/launch_chrome_debug.sh --account $(or $(ACCOUNT),default)

chrome-stop: ## stop that account's Chrome: make chrome-stop [ACCOUNT=work]
	bash tools/launch_chrome_debug.sh --account $(or $(ACCOUNT),default) --stop

accounts: ## list the accounts in ~/.facebook-mcp
	$(PY) tools/export_cookies.py --list

clean: ## remove build/test artefacts
	rm -rf dist build .pytest_cache .ruff_cache
	find . -name '*.egg-info' -maxdepth 2 -prune -exec rm -rf {} +
	find . -name '__pycache__' -not -path './.venv/*' -prune -exec rm -rf {} +
