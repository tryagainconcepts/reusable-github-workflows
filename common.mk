# common make file
# add `include common.mk` to your Makefile

.DEFAULT_GOAL := help

.PHONY: help format lint test clean clean-build clean-pyc config config-test config-develop release-s3 upgrade-packages

help: ## Show available targets.
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_-]+:.*## / {printf "%-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

format: ## Format and apply configured pre-commit fixes.
	@test -f .pre-commit-config.yaml || curl -sSfO https://raw.githubusercontent.com/tryagainconcepts/reusable-github-workflows/main/.pre-commit-config.yaml
	# pre-commit runs hooks in order, once each, and exits 1 whenever a hook
	# modifies a file. A fix from a later hook (ruff-check/ruff-format) can create
	# work for an earlier one (trailing-whitespace, end-of-file-fixer), so a
	# single pass is not always enough. Re-run until a pass makes no changes
	# (bounded); the final pass then only fails on issues hooks cannot auto-fix.
	@for i in 1 2 3 4 5; do \
		echo "pre-commit pass $$i"; \
		if uvx pre-commit run --all-files; then exit 0; fi; \
	done; \
	echo "pre-commit still reports issues after 5 passes"; exit 1

# Keep the ruff pin in sync with the ruff-pre-commit rev in
# .pre-commit-config.yaml; an unpinned uvx ruff resolves to whatever is
# latest at install time, so local and CI results drift apart.
RUFF_VERSION := 0.16.3

lint: ## Run Ruff and all configured pre-commit checks.
	uvx ruff@$(RUFF_VERSION) check .
	uvx pre-commit run --all-files

test: lint ## Run the test suite in the locked environment (parallel when pytest-xdist is installed).
	@uv run --no-sync python -c "import xdist" 2>/dev/null \
	  && flags="-n auto --dist loadfile" \
	  || { flags=""; echo "pytest-xdist not installed; running serially"; }; \
	set -x; uv run --no-sync python -m pytest $$flags -x --log-level=INFO -l tests

clean: clean-build clean-pyc ## Remove generated build and Python artifacts.

clean-build: ## Remove package build artifacts.
	rm -rf build dist
	find . -name '*.egg-info' -not -path './.venv/*' -exec rm -rf {} +

clean-pyc: ## Remove Python bytecode and backup files.
	find . -name '*.pyc' -delete
	find . -name '*.pyo' -delete
	find . -name '*~' -delete

config: ## Install locked production dependencies.
	uv sync --frozen --no-dev

config-test: ## Install locked dependencies with all extras.
	uv sync --frozen --all-extras

config-develop: config-test ## Install development tools, hooks, and the latest shared config.
	curl -sSfO https://raw.githubusercontent.com/tryagainconcepts/reusable-github-workflows/main/.pre-commit-config.yaml
	curl -sSfO https://raw.githubusercontent.com/tryagainconcepts/reusable-github-workflows/main/common.mk
	uvx pre-commit install

release-s3: clean ## Build and upload the package to the private S3 index.
	uv build
	uvx --from s3pypi s3pypi --verbose upload dist/* --bucket pipy.detalytics.com --put-root-index

upgrade-packages: ## Upgrade the lockfile and publish the automation branch.
	uv lock --upgrade
	@if git diff --quiet uv.lock; then \
		echo "lockfile unchanged; nothing to push"; \
	else \
		git commit -m 'automated package update' uv.lock && \
		git push -f origin HEAD:automated-package-update; \
	fi
