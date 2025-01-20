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
	pipenv run pre-commit run --all-files

lint:
	pipenv run ruff check .

test: lint ## run tests quickly with the default Python
	pipenv run python -m pytest -v -l tests

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
	pipenv install --dev

config-develop:
	pipenv install --dev
	pipenv run pip install pre-commit
	curl -O https://raw.githubusercontent.com/tryagainconcepts/reusable-github-workflows/main/.pre-commit-config.yaml
	curl -O https://raw.githubusercontent.com/tryagainconcepts/reusable-github-workflows/main/pyproject.toml
	curl -O https://raw.githubusercontent.com/tryagainconcepts/reusable-github-workflows/main/common.mk
	pipenv run pre-commit install
	pipenv run pre-commit run --all-files

config:
	pipenv install

release-s3: clean
	pipenv run pip install setuptools wheel
	pipenv run python setup.py sdist bdist_wheel
	s3pypi upload dist/* --bucket pipy.detalytics.com --put-root-index