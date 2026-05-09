# 06 — llms.txt, llms-full.txt, CLAUDE.md, AGENTS.md

AI coding agents (Claude Code, Copilot, Cursor, etc.) need machine-readable
documentation to work effectively in a codebase.  Companion packages must
ship four files that mirror the pattern used in `lauren-framework` and
`lauren-ai`.

## `llms.txt` — 2 KB overview

Placed at repo root.  Should be ≤ 2 KB.  Follows the [llmstxt.org](https://llmstxt.org) convention.

```markdown
# lauren-cache

> Redis/in-process caching companion for the Lauren framework.

lauren-cache adds a `CacheModule` with in-process and Redis-backed caching
to any Lauren application.  It integrates through Lauren's DI container —
inject `CacheService` anywhere `@injectable` is resolved.

## Quick start

    pip install lauren-cache[redis]

    from lauren_cache import CacheModule, CacheConfig

    @module(imports=[CacheModule.for_root(CacheConfig(default_ttl_seconds=300))])
    class AppModule: pass

## Key symbols

- `CacheModule` — module factory, call `.for_root(config)` or `.for_testing()`
- `CacheService` — injectable singleton; `get / set / delete`
- `CacheConfig` — dataclass: `default_ttl_seconds`, `max_entries`, `namespace`

## Links

- Full reference: https://raw.githubusercontent.com/…/llms-full.txt
- Docs: https://github.com/lauren-framework/lauren-cache
- Source: src/lauren_cache/
```

## `llms-full.txt` — complete API reference

Placed at `src/<package>/llms-full.txt` (served at the package level) **and**
at repo root.  Format: free-form Markdown with one section per public symbol.

```markdown
# lauren-cache — complete reference

## CacheModule

```python
class CacheModule
```

Module factory for the caching system.

### `CacheModule.for_root`

```python
@classmethod
def for_root(cls, config: CacheConfig | None = None) -> type
```

Returns a wired `@module` ready for `imports=[]`.  If `config` is None
uses `CacheConfig()` defaults.  Exports `CacheService`.

### `CacheModule.for_testing`

```python
@classmethod
def for_testing(cls) -> type
```

Returns a module with `CacheConfig(default_ttl_seconds=0)` — all
entries expire immediately, making test assertions deterministic.

---

## CacheService

```python
@injectable(scope=Scope.SINGLETON)
class CacheService
```

Application-wide cache.  Inject via constructor: `def __init__(self, cache: CacheService)`.

### `CacheService.get`

```python
async def get(self, key: str) -> object | None
```

Returns the cached value or `None` on miss or expiry.

### `CacheService.set`

```python
async def set(self, key: str, value: object, ttl: int | None = None) -> None
```

Stores `value` under `key`.  `ttl` overrides `CacheConfig.default_ttl_seconds`.

### `CacheService.delete`

```python
async def delete(self, key: str) -> None
```

Removes `key` from the cache.  No-op if not present.
```

## `scripts/check_llms_full.py`

Adapt from `lauren-framework/scripts/check_llms_full.py` — only the package
name and path change:

```python
#!/usr/bin/env python3
"""Verify every public symbol in `lauren_cache.__all__` is in llms-full.txt."""

from __future__ import annotations
import importlib, pathlib, sys

def main(argv: list[str]) -> int:
    pkg = importlib.import_module("lauren_cache")
    public = set(getattr(pkg, "__all__", ()) or ())
    if not public:
        print("ERROR: lauren_cache.__all__ is empty", file=sys.stderr)
        return 2

    source_dir = pathlib.Path(__file__).parent.parent
    llms_path = source_dir / "src" / "lauren_cache" / "llms-full.txt"
    if not llms_path.exists():
        # Fall back to repo root
        llms_path = source_dir / "llms-full.txt"
    if not llms_path.exists():
        print("ERROR: llms-full.txt not found", file=sys.stderr)
        return 2

    text = llms_path.read_text(encoding="utf-8")
    missing = sorted(name for name in public if name not in text)
    if missing:
        print("ERROR: missing from llms-full.txt:")
        for name in missing:
            print(f"  - {name}")
        return 1

    print(f"OK: all {len(public)} public symbols are in llms-full.txt")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

## `nox -s llms_check`

Add this session to `noxfile.py`:

```python
@nox.session(python=PRIMARY_PYTHON, reuse_venv=True, name="llms_check")
def llms_check(session: nox.Session) -> None:
    """Verify every public symbol in lauren_cache is in llms-full.txt."""
    session.install("-e", ".")
    session.run("python", str(ROOT / "scripts" / "check_llms_full.py"), *session.posargs)
```

Wire the prek hook in `.pre-commit-config.yaml`:

```yaml
- repo: local
  hooks:
    - id: llms-full-txt-sync
      name: llms-full.txt is in sync with lauren_cache.__all__
      entry: python scripts/check_llms_full.py
      language: python
      pass_filenames: false
      files: ^(src/lauren_cache/.*\.py|llms-full\.txt)$
      additional_dependencies:
        - "-e ."
```

## `CLAUDE.md`

Coding assistant instructions — read by Claude Code automatically.  Minimum
required sections:

```markdown
# CLAUDE.md

## Commands

    nox                   # lint + tests + typecheck
    nox -s tests_unit     # unit tests only
    nox -s tests_integration  # integration tests
    nox -s docs_serve     # live-reload docs

## Architecture

- `src/lauren_cache/` — package source
- `tests/unit/` — isolated unit tests (no DI container)
- `tests/integration/` — full LaurenFactory.create() + TestClient flows
- `scripts/` — docs generation + llms-full.txt sync check

## Golden rules

1. Add every new public name to `__all__` AND `llms-full.txt`.
2. `@injectable(scope=SINGLETON)` for shared state; `REQUEST` for per-request.
3. Never raise from `@pre_destruct` — swallow and log.
4. `nox` must be green before opening a PR.
```

## `AGENTS.md`

Task-lookup table for non-Claude AI agents (Cursor, Copilot, etc.):

```markdown
# AGENTS.md

## Quick task lookup

| Task | Where to look |
|---|---|
| Add a new cached operation | `src/lauren_cache/_service.py` |
| Change default TTL | `src/lauren_cache/_config.py` |
| Add a Redis backend | `src/lauren_cache/_redis.py` (new file) + `_module.py` |
| Wire a new optional dep | `pyproject.toml [project.optional-dependencies]` |

## Definition of done

- [ ] `nox` passes (lint + tests + typecheck)
- [ ] New public name added to `__all__` and `llms-full.txt`
- [ ] Integration test covers the new behaviour
- [ ] CHANGELOG.md updated under `[Unreleased]`
```
