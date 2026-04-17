# pyvista/admin local development targets.
#
# Peribolos itself only runs in CI (see .github/workflows/). These targets
# cover the local-machine work: installing pre-commit, running the linters
# and the live-org consistency check before opening a PR.

.DEFAULT_GOAL := help
SHELL := bash

.PHONY: help install lint fmt ci check clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install pre-commit git hooks (one-time)
	pre-commit install

lint: ## Run all pre-commit hooks on all files
	pre-commit run --all-files

fmt: lint ## Alias for lint (pre-commit auto-fixes what it can)

check: ## Validate org.yaml against live org state. Requires GITHUB_TOKEN.
	uv run scripts/sync-repos.py --check > /dev/null
	@echo "org.yaml is consistent with live GitHub state"

ci: lint check ## Everything CI runs locally: lint + live-org consistency check

clean: ## Remove generated files
	rm -f org-expanded.yaml
