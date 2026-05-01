# Contributing to `lauren`

Thanks for taking the time to contribute. Whether you're a human or
an AI coding agent, this document contains everything you need to
submit a change that will land cleanly.

## 1. Setup

```bash
git clone <repo-url>
cd lauren-framework
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

If all tests pass, you're ready to go.

## 2. Design Philosophy (Read First)

Please read `.CLAUDE.md` — it is the canonical source of design
invariants. The short version:

- **Startup validates; runtime dispatches.** Any misuse must be
  rejected by `LaurenFactory.create(...)`, not on first request.
- **Decorators attach metadata; they never rewrite functions.**
- **No reflection on the request path.** Pre-compile everything.
- **Every public name is reflected in `__all__` and `llms-full.txt`.**

A pull request that violates these will not be merged even if it
passes tests.

## 3. Branching & Commits

- Branch from `main`. Use `feat/<short-name>`, `fix/<short-name>`, or
  `docs/<short-name>`.
- Commit messages: `<scope>: <imperative sentence>`. Scopes are one
  of `di`, `asgi`, `routing`, `ws`, `typing`, `extractors`, `tests`,
  `docs`, `meta`.

  Good: `typing: resolve ForwardRef annotations via _typing sub-package`
  Bad:  `add forwardref support and fix some tests and update docs`

- Squash before merging unless the history genuinely tells a story.

## 4. Tests

- Every new feature requires at least one test in the matching
  `tests/unit/` or `tests/integration/` directory.
- Every bug fix requires a regression test that fails on `main` and
  passes on the branch.
- `pytest -q` must stay green at every commit on your branch.
- Benchmarks go under `tests/benchmarks/` and are marked
  `@pytest.mark.benchmark`. They are excluded from the default run
  (see `nox -s benchmark`).

## 5. Documentation

Two files are machine-readable and must be kept in sync with the
public API:

- `README.md` — the narrative introduction for human readers.
- `llms-full.txt` — the exhaustive reference shipped inside the
  package for LLM consumption.

Adding a public name without updating both counts as an incomplete
change.

## 6. Code Style

- Black-compatible formatting. No separate formatter required; the
  style is close enough that a pre-commit hook isn't warranted.
- `from __future__ import annotations` at the top of every module.
- PEP 604 unions (`int | None`), not `Optional[int]` in new code.
- Private names carry a leading underscore. Private classes use
  `_PascalCase`.

## 7. Adding a New Public API

1. Write a failing integration test that exercises the new shape.
2. Implement the minimum change that makes it pass.
3. If the feature can be misused at startup, add a specific error
   subclass in `exceptions.py` and a test that triggers it.
4. Export the new name from `lauren/__init__.py::__all__`.
5. Document the name in `llms-full.txt` with at least one example.
6. Mention the name in `README.md` if it's likely to appear in a
   user's first hour with the framework.

## 8. Recent Features

This section summarises significant additions since the first stable
release.  When you work on these areas, read the linked guide first.

### 8.1 Unified Extractor Signature + `ExecutionContext`

All extractor classes now implement a single canonical instance-method:

```python
async def extract(self, execution_context: ExecutionContext, extraction: Extraction): ...
```

`ExecutionContext` carries `request`, `handler_class`, `handler_func`,
`route_template`, and `metadata` — the same context object guards
receive.  An extractor that needs DI-injected dependencies must be
decorated with `@injectable(scope=Scope.SINGLETON)` (or REQUEST/TRANSIENT);
non-injectable extractors are instantiated once (no-arg) and cached
process-wide.

**Guide**: `docs/guides/custom-extractors.md`

**Tests**: `tests/unit/test_extractors.py::TestUnifiedExtractorSignature`,
`tests/integration/test_extractor_integration.py::TestExecutionContextInExtractors`

### 8.2 Implicit Parameter Detection

Handler parameters that carry no explicit extractor annotation are
automatically promoted at startup:

| Condition | Promoted to |
|-----------|-------------|
| Name matches `{segment}` in URL template | `Path[T]` |
| Annotation is a Pydantic `BaseModel` | `Json[T]` (request body) |
| Annotation is a scalar (`int`, `str`, `float`, `bool`, `bytes`) | `Query[T]` |

`Query[SomeModel]` is also supported: field values are collected from
the query string by field name (or alias) and validated by Pydantic.

Unresolvable parameters raise `UnresolvableParameterError` at startup,
never at request time.

**Guide**: `docs/guides/implicit-params.md`

**Tests**: `tests/unit/test_implicit_params.py`,
`tests/integration/test_implicit_params_integration.py`

### 8.3 `IS_PUBLIC_KEY` and the `@public` Decorator

Guards that ship with `lauren` (and recommended third-party guards) all
honour a shared metadata key:

```python
from lauren_guards import IS_PUBLIC_KEY, public

@get("/health")
@public          # sets IS_PUBLIC_KEY = True on the route metadata
async def health(self) -> Response: ...
```

Any guard that calls `ctx.get_metadata(IS_PUBLIC_KEY)` at the top of
`can_activate` will skip its check for public routes, enabling
cooperation between unrelated guards without coupling their
implementations.

**Reference**: `lauren_guards.IS_PUBLIC_KEY = "lauren-guards.authentication.is_public"`

### 8.4 OpenAPI Security from Guards

Guards can now advertise their OpenAPI security requirement by
implementing `openapi_security() -> list[dict]`:

```python
class MyBearerGuard:
    def openapi_security(self) -> list[dict]:
        return [{"BearerAuth": []}]

    async def can_activate(self, ctx: GuardContext) -> bool: ...
```

The framework merges these declarations into the generated schema.
Priority: route-level guard > controller-level guard > global guard.

**Guide**: `docs/guides/openapi-security.md`

### 8.5 `LaurenFactory.create()` is Synchronous

`LaurenFactory.create(root_module, ...)` is a **synchronous** static
method.  All seven compilation phases are CPU-bound and happen at
module load time (safe for uvicorn import-time setup).  `startup()` is
called separately:

```python
# Production (uvicorn handles the lifespan):
app = LaurenFactory.create(AppModule)

# Tests:
app = LaurenFactory.create(AppModule)
await app.startup()
```

`TestClient.__init__` calls `startup()` automatically if the app hasn't
started yet.

### 8.6 `ExtractionMarker` and `Extraction` (Public Aliases)

`_ExtractionMarker` → `ExtractionMarker` and `_Extraction` → `Extraction`.
Both are now public and exported from `lauren`.  Existing code that
referenced the private names will continue to work (backward-compat
aliases remain), but new code should use the public names.

## 9. AI-Assisted Contributions

AI coding agents are welcome. See `AGENTS.md` for the specific
operating instructions. Pull requests authored with agent assistance
should be tagged `ai-assisted` in the description; this is not a
gatekeeping flag, just a signal for reviewers.

## 10. Reporting Issues

When reporting a bug:

- Include a minimal reproduction as Python code, not a description.
- State the Python version and the installed pydantic version.
- Paste the full traceback, including the `detail` dict on any
  `LaurenError` subclass.
- If the bug is about decoration order, include the decorator stack
  as it appeared in your source.

## 11. Code of Conduct

Be kind and specific. Review comments should point at a line, a
reason, and a suggested change. "This is wrong" without elaboration
is not a review comment.
