# 05 — GitHub Actions Workflows

Companion packages use the same **six-workflow** pattern as `lauren-framework`
and `lauren-ai`.  All workflows call `nox` sessions, so they stay in sync with
the local developer experience automatically.

## The six files

```
.github/workflows/
├── tests.yml      ← unit + integration + coverage matrix
├── lint.yml       ← prek (all hooks) + mypy + llms-txt-sync
├── docs.yml       ← build docs site (strict mode)
├── codeql.yml     ← weekly CodeQL security scan
├── stale.yml      ← auto-close inactive issues/PRs
└── release.yml    ← build → validate → TestPyPI / PyPI
```

## `tests.yml`

```yaml
name: tests
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  schedule:
    - cron: "23 4 * * *"   # nightly regression run
  workflow_dispatch:

concurrency:
  group: tests-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  unit:
    name: unit · py${{ matrix.python }}
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python: ["3.11", "3.12", "3.13", "3.14"]
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - run: pip install nox
      - run: nox -s tests_unit -- -q

  integration:
    name: integration · py${{ matrix.python }}
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python: ["3.11", "3.12", "3.13", "3.14"]
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - run: pip install nox
      - run: nox -s tests_integration -- -q

  coverage:
    name: coverage
    runs-on: ubuntu-latest
    needs: [unit, integration]
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install nox
      - run: nox -s coverage -- --cov-report=xml --cov-report=term
      - uses: actions/upload-artifact@v4
        with:
          name: coverage-xml
          path: coverage.xml
          if-no-files-found: warn
          retention-days: 14
      - name: Upload to Codecov
        if: env.CODECOV_TOKEN != ''
        uses: codecov/codecov-action@v4
        env:
          CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }}
        with:
          files: coverage.xml
          fail_ci_if_error: false
```

## `lint.yml`

```yaml
name: lint
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: lint-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  prek:
    name: prek (all hooks)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: actions/cache@v4
        with:
          path: ~/.cache/prek
          key: prek-${{ runner.os }}-${{ hashFiles('.pre-commit-config.yaml') }}
          restore-keys: prek-${{ runner.os }}-
      - run: pip install "prek>=0.3"
      - run: prek run --all-files --show-diff-on-failure --color always

  mypy:
    name: mypy
    runs-on: ubuntu-latest
    continue-on-error: ${{ github.event_name == 'pull_request' }}
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install nox
      - run: nox -s typecheck

  llms-txt-sync:
    name: llms-full.txt is in sync
    runs-on: ubuntu-latest
    continue-on-error: ${{ github.event_name == 'pull_request' }}
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install nox
      - run: nox -s llms_check
```

## `release.yml`

```yaml
name: release
on:
  push:
    tags:
      - "v*.*.*"
  workflow_dispatch:
    inputs:
      target:
        description: "Where to publish"
        required: true
        default: "testpypi"
        type: choice
        options: [testpypi, pypi]

permissions:
  contents: read

jobs:
  build:
    name: Build sdist + wheel
    runs-on: ubuntu-latest
    outputs:
      version: ${{ steps.meta.outputs.version }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Install uv
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install nox setuptools-scm
      - id: meta
        shell: bash
        run: |
          if [[ "${GITHUB_REF}" == refs/tags/v* ]]; then
            version="${GITHUB_REF#refs/tags/v}"
          else
            version="$(python -m setuptools_scm 2>/dev/null || echo dev)"
          fi
          echo "version=${version}" >> "${GITHUB_OUTPUT}"
      - run: nox -s llms_check
      - run: nox -s build
      - run: nox -s build_check
      - uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist/
          retention-days: 14

  publish-testpypi:
    name: Publish → TestPyPI
    needs: build
    runs-on: ubuntu-latest
    if: github.event_name == 'workflow_dispatch' && inputs.target == 'testpypi'
    environment:
      name: testpypi
      url: https://test.pypi.org/project/{{PACKAGE}}/   # ← replace placeholder
    permissions:
      id-token: write
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: dist
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          repository-url: https://test.pypi.org/legacy/
          packages-dir: dist/
          skip-existing: true

  publish-pypi:
    name: Publish → PyPI
    needs: build
    runs-on: ubuntu-latest
    if: |
      startsWith(github.ref, 'refs/tags/v') ||
      (github.event_name == 'workflow_dispatch' && inputs.target == 'pypi')
    environment:
      name: pypi
      url: https://pypi.org/project/{{PACKAGE}}/         # ← replace placeholder
    permissions:
      id-token: write
      contents: write
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: dist
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          packages-dir: dist/
      - name: Create GitHub Release
        if: startsWith(github.ref, 'refs/tags/v')
        uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
          files: dist/*
```

## nox session ↔ CI job mapping

| nox session | CI workflow | Trigger |
|---|---|---|
| `tests_unit` | `tests.yml / unit` | every PR + push to main |
| `tests_integration` | `tests.yml / integration` | every PR + push to main |
| `coverage` | `tests.yml / coverage` | after unit + integration |
| `lint` | `lint.yml / prek` (via prek hook) | every PR + push to main |
| `typecheck` | `lint.yml / mypy` | every PR + push to main |
| `llms_check` | `lint.yml / llms-txt-sync` | every PR + push to main |
| `docs` | `docs.yml / build` | changes to docs/ or src/ |
| `build` + `build_check` | `release.yml / build` | tag push or manual dispatch |

## Adding Codecov

1. Connect the repo at [codecov.io](https://codecov.io)
2. Add `CODECOV_TOKEN` as a GitHub Actions secret
3. The `coverage` job already calls `codecov/codecov-action` — it activates
   automatically once the secret is present

## Skipping expensive eval tests in CI

Mark evaluation tests so they never run in the standard matrix:

```python
@pytest.mark.eval
async def test_agent_accuracy():
    """Requires ANTHROPIC_API_KEY — skipped in default CI."""
    ...
```

In `noxfile.py` the `eval_` session runs only these:

```python
@nox.session(python=PRIMARY_PYTHON)
def eval_(session):
    _install_dev(session)
    session.run("pytest", "-m", "eval", str(TESTS_DIR / "eval"), "-v", *session.posargs)
```

Add an optional CI job triggered only on workflow_dispatch (never in the matrix).
