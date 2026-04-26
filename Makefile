# Makefile — deprecated thin shim that forwards to noxfile.py.
#
# `nox` is now the canonical task runner for lauren — see noxfile.py for the
# full set of sessions. This Makefile exists only so that `make <target>`
# muscle-memory still works.
#
# Discover everything:  nox -l   (or)   nox -s help
# Run defaults (lint + tests + typecheck):  nox
#
# Each target below maps 1:1 to a nox session.

NOX ?= nox

.PHONY: help install test test-unit test-integration test-verbose \
        coverage lint format typecheck clean \
        docs docs-install docs-serve \
        build build-check release release-test \
        ci llms-check

help:  ## Show this help message
	@$(NOX) -s help 2>/dev/null || $(NOX) -l

install:  ## Install the package in editable mode with dev extras
	pip install -e ".[dev]"

test:  ## Run the full test suite (alias for `nox -s tests`)
	$(NOX) -s tests

test-unit:  ## Run only unit tests
	$(NOX) -s tests_unit

test-integration:  ## Run only integration tests
	$(NOX) -s tests_integration

test-verbose:  ## Run the full test suite with verbose output
	$(NOX) -s tests_verbose

coverage:  ## Run tests under coverage with a terminal summary
	$(NOX) -s coverage

lint:  ## Run ruff
	$(NOX) -s lint

format:  ## Auto-format the codebase with ruff
	$(NOX) -s format

typecheck:  ## Run mypy
	$(NOX) -s typecheck

clean:  ## Remove build artefacts and caches
	$(NOX) -s clean

docs-install:  ## Install MkDocs + Material theme
	$(NOX) -s docs_install

docs:  ## Build the documentation site (strict)
	$(NOX) -s docs

docs-serve:  ## Serve the docs locally with live reload
	$(NOX) -s docs_serve

build:  ## Build wheel + sdist into ./dist
	$(NOX) -s build

build-check:  ## Validate the built distributions with twine check
	$(NOX) -s build_check

release-test:  ## Upload wheel + sdist to TestPyPI
	$(NOX) -s release_test

release:  ## Upload wheel + sdist to the real PyPI (requires `-- --yes`)
	$(NOX) -s release -- --yes

ci:  ## Run the full CI matrix locally
	$(NOX) -s ci

llms-check:  ## Verify llms-full.txt is in sync with lauren.__all__
	$(NOX) -s llms_check
