---
name: building-companion-packages
description: >
  Everything needed to build, test, release, and maintain a first-party or
  third-party companion package for the Lauren framework — using lauren-ai as
  the canonical reference implementation.
---

# Building Companion Packages

A **companion package** extends Lauren with a new capability domain (LLM agents,
caching, queuing, observability, …) without bloating the core.  It ships as its
own PyPI package, depends on `lauren`, and integrates through Lauren's DI
container and module system.

**Reference implementation:** [`lauren-ai`](https://github.com/lauren-framework/lauren-ai)
— the official AI/LLM companion that adds `@agent`, `@tool`, `LLMModule`, and
the full multi-model transport layer.

---

## Use this skill when you want to

- Publish a standalone package that adds features to Lauren apps
- Create an internal library that re-uses Lauren's DI and module system
- Contribute a new domain package to the `lauren-framework` organisation
- Scaffold a new companion quickly from `lauren-package-template`

---

## Contents

| File | What it covers |
|---|---|
| [`01-package-structure.md`](01-package-structure.md) | Directory layout, `pyproject.toml` template, naming conventions, `py.typed`, `setuptools-scm` |
| [`02-di-integration.md`](02-di-integration.md) | `@injectable`, scopes, module wiring, `exports=`, full `CacheModule` worked example |
| [`03-module-factory.md`](03-module-factory.md) | `.for_root()` / `.for_testing()` patterns, `use_value` / `use_class` / `use_factory` inside a factory |
| [`04-testing.md`](04-testing.md) | Pytest layout, `conftest.py` patterns, DI isolation, coverage setup, `benchmark` / `eval` markers |
| [`05-github-workflows.md`](05-github-workflows.md) | The six-workflow CI/CD pattern, `nox` sessions ↔ jobs, OIDC publishing, Codecov |
| [`06-llms-txt.md`](06-llms-txt.md) | `llms.txt`, `llms-full.txt`, `scripts/check_llms_full.py`, `CLAUDE.md`, `AGENTS.md`, `nox -s llms_check` |
| [`07-publishing.md`](07-publishing.md) | Semantic versioning, OIDC Trusted Publishing, GitHub Releases, `skills/` distribution |

---

## Quick-start with `lauren-package-template`

The fastest path is to clone the official template repository, which pre-wires
all of the above:

```bash
# 1. Use the GitHub template (or clone directly)
git clone https://github.com/lauren-framework/lauren-package-template my-package
cd my-package

# 2. Replace placeholders
export PACKAGE="lauren-cache"
export MODULE="lauren_cache"
grep -rl '{{PACKAGE}}' . | xargs sed -i "s/{{PACKAGE}}/$PACKAGE/g"
grep -rl '{{MODULE}}' . | xargs sed -i "s/{{MODULE}}/$MODULE/g"
mv src/lauren_PACKAGE "src/$MODULE"

# 3. Install and smoke-test
uv sync --extra dev
prek install
nox                       # lint + tests + typecheck
```

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `MissingProviderError` at startup | Companion provider not listed in `providers=` or `imports=` | Add to the module's `providers=` or export from a sub-module and import it |
| `ModuleExportViolation` | Trying to export a token the module doesn't own | Only export tokens declared in this module's `providers=` |
| `DIScopeViolationError` | SINGLETON depending on REQUEST-scoped companion service | Make the dependent service REQUEST-scoped too, or restructure |
| `llms-full.txt out of sync` | Added a name to `__all__` without updating `llms-full.txt` | Run `nox -s llms_check` locally to see what's missing, then update the file |
| CI fails on `nox -s tests` with import error | `lauren` not installed editable in CI | Ensure `pyproject.toml` has `[tool.uv.sources] lauren = {path="../lauren-framework", editable=true}` |
