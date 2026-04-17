# pyvista/admin local development targets.
#
# Most day-to-day maintenance is: edit org.yaml, open a PR, let CI run.
# These targets exist for local testing, one-off validation, and the rare
# break-glass apply. GITHUB_TOKEN must be exported for targets that talk to
# the live org (check, expand, dry-run, apply).

.DEFAULT_GOAL := help
SHELL := bash

.PHONY: help install lint fmt ci expand check dry-run apply clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install pre-commit git hooks (one-time)
	pre-commit install

lint: ## Run all pre-commit hooks on all files
	pre-commit run --all-files

fmt: lint ## Alias for lint (pre-commit auto-fixes what it can)

ci: lint check ## Everything CI runs: lint + live-org consistency check

expand: ## Write org-expanded.yaml (peribolos-ready). Requires GITHUB_TOKEN.
	uv run scripts/sync-repos.py --output org-expanded.yaml

check: ## Validate org.yaml against live org state. Requires GITHUB_TOKEN.
	uv run scripts/sync-repos.py --check > /dev/null
	@echo "org.yaml is consistent with live GitHub state"

dry-run: ## Preview what peribolos would change. Requires GITHUB_TOKEN + docker.
	scripts/run-peribolos.sh dry-run

apply: ## Apply org.yaml to GitHub. DANGEROUS. Requires CONFIRM=yes + GITHUB_TOKEN + docker.
	@if [ "$$CONFIRM" != "yes" ]; then \
		echo "Refusing to apply. This modifies the live pyvista org."; \
		echo "Re-run with: make apply CONFIRM=yes"; \
		exit 1; \
	fi
	scripts/run-peribolos.sh apply

clean: ## Remove generated files
	rm -f org-expanded.yaml
