# 01 — Package Structure

## Directory layout

Every Lauren companion package uses the **src layout**.  This prevents the
package from being importable from the project root (which would shadow an
editable install and cause subtle import-order bugs).

```
lauren-cache/                         ← repo root (kebab-case)
├── .github/
│   └── workflows/                    ← 6 files — see 05-github-workflows.md
├── docs/
│   ├── index.md
│   └── guides/
├── scripts/
│   ├── check_llms_full.py            ← verifies __all__ ↔ llms-full.txt
│   └── generate_api_docs.py          ← writes docs/generated-reference/
├── src/
│   └── lauren_cache/                 ← snake_case — importable name
│       ├── __init__.py               ← __all__ + re-exports
│       └── py.typed                  ← PEP 561: "this package ships stubs"
├── tests/
│   ├── unit/
│   └── integration/
├── skills/                           ← agent-readable skill guides
├── .editorconfig
├── .gitignore
├── .pre-commit-config.yaml
├── AGENTS.md
├── CHANGELOG.md
├── CLAUDE.md
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── docs-requirements.txt
├── llms.txt
├── llms-full.txt
├── mkdocs.yml
├── noxfile.py
├── pyproject.toml
└── uv.lock
```

## Naming conventions

| Aspect | Convention | Example |
|---|---|---|
| Repo name | `lauren-<domain>` | `lauren-cache` |
| PyPI name | `lauren-<domain>` | `lauren-cache` |
| Python package | `lauren_<domain>` | `lauren_cache` |
| Module prefix | `Lauren<Domain>` | `CacheModule` |
| Factory method | `<Module>.for_root(cfg)` | `CacheModule.for_root(cfg)` |

## `pyproject.toml` template

```toml
[build-system]
requires = ["setuptools>=72", "setuptools-scm>=8.0"]
build-backend = "setuptools.build_meta"

[project]
name = "lauren-cache"                   # ← change
dynamic = ["version"]
description = "Redis/in-process caching companion for the Lauren framework"
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.11"
authors = [{ name = "Lauren Contributors" }]
keywords = ["lauren", "cache", "redis"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Programming Language :: Python :: 3.14",
    "Typing :: Typed",
]

# Hard dependencies: lauren + whatever your package always needs.
# Keep this list minimal; put optional backends in extras.
dependencies = [
    "lauren",
    "anyio>=4.0",
]

[project.optional-dependencies]
# Optional backends / integrations
redis   = ["redis[hiredis]>=5.0"]
memcached = ["aiomcache>=0.8"]
all     = ["lauren-cache[redis,memcached]"]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "ruff>=0.6",
    "mypy>=1.10",
    "nox>=2024.4",
    "prek>=0.3",
    "mkdocs>=1.6",
    "mkdocs-material>=9.5",
    "mkdocstrings[python]>=0.27",
    "griffe>=1.0",
]

[project.urls]
Homepage   = "https://github.com/lauren-framework/lauren-cache"
Repository = "https://github.com/lauren-framework/lauren-cache"

# ── Version from git tags (e.g. v1.2.3 → "1.2.3") ──────────────────────────
[tool.setuptools_scm]
fallback_version = "0.0.0+unknown"
version_scheme = "post-release"
local_scheme = "dirty-tag"

[tool.setuptools.packages.find]
where = ["src"]

# ── Local dev: resolve `lauren` from sibling directory ──────────────────────
# Remove this section when publishing; PyPI resolves lauren from the index.
[tool.uv.sources]
lauren = { path = "../lauren-framework", editable = true }

# ── Tools ───────────────────────────────────────────────────────────────────
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "ANN"]
ignore = ["ANN101", "ANN102", "ANN401", "B008"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["ANN", "S101"]
"noxfile.py" = ["ANN"]

[tool.mypy]
python_version = "3.11"
strict = true
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-m 'not benchmark and not eval'"
markers = [
    "benchmark: excluded from default run",
    "eval: requires API keys, excluded from default run",
]
testpaths = ["tests"]
pythonpath = ["src", "../lauren-framework"]

[tool.coverage.run]
source = ["src/lauren_cache"]
omit = ["tests/*"]

[tool.coverage.report]
fail_under = 80
show_missing = true
```

## `src/lauren_cache/__init__.py`

```python
"""lauren-cache — Redis/in-process caching for Lauren applications."""

from __future__ import annotations

from importlib.metadata import version

from ._module import CacheModule
from ._service import CacheService, CacheConfig

__all__ = [
    "CacheModule",
    "CacheService",
    "CacheConfig",
]

__version__: str = version("lauren-cache")
```

## `src/lauren_cache/py.typed`

Empty file — tells type-checkers that this package ships type information.

## `uv.lock`

Committed to the repo.  Generated automatically by `uv sync`.  Do not edit by hand.

## `.gitignore` essentials

```
__pycache__/
*.pyc
.venv/
.nox/
dist/
build/
*.egg-info/
site/
.coverage
htmlcov/
coverage.xml
```
