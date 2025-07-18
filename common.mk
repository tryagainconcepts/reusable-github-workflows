# common make file
# add include common.mk to your Makefile

.PHONY: clean-pyc clean-build docs
help:
	@echo "clean-build - remove build artifacts"
	@echo "clean-pyc - remove Python file artifacts"
	@echo "config - install package and production dependencies"
	@echo "config-test - install package and test dependencies"
	@echo "config-develop - install package and development dependencies"
	@echo "coverage - run coverage test"
	@echo "format - format code with black"
	@echo "lint - check style with pylint"
	@echo "test - run tests"
	@echo "docs - generate Sphinx HTML documentation, including API docs"
	@echo "release - package and upload a release"
	@echo "sdist - package"


format:
	curl -O https://raw.githubusercontent.com/tryagainconcepts/reusable-github-workflows/main/.pre-commit-config.yaml
	uvx pre-commit run --all-files

lint:
	uv pip install ruff
	uvx ruff check . --exclude "*.ipynb"
	uvx pre-commit install
	uvx pre-commit run --all-files

test: lint ## run tests quickly with the default Python
	uvx python -m pytest -v -l tests

clean: clean-build clean-pyc

clean-build:
	rm -fr build/
	rm -fr dist/
	rm -fr *.egg-info

clean-pyc:
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +



config-test:
	uv pip install ".[dev]"

config-develop:
	uv pip install ".[dev]"
	uv pip install pre-commit
	curl -O https://raw.githubusercontent.com/tryagainconcepts/reusable-github-workflows/main/.pre-commit-config.yaml
	curl -O https://raw.githubusercontent.com/tryagainconcepts/reusable-github-workflows/main/pyproject.toml
	curl -O https://raw.githubusercontent.com/tryagainconcepts/reusable-github-workflows/main/common.mk
	uvx pre-commit install
	uvx pre-commit run --all-files

config:
	uv pip install .

release-s3: clean
	uv pip install setuptools wheel s3pypi
	uv run python setup.py sdist bdist_wheel
	uvx s3pypi --verbose upload dist/* --bucket pipy.detalytics.com --put-root-index