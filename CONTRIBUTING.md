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

If all 550+ tests pass in under five seconds, you're ready to go.

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

## 8. AI-Assisted Contributions

AI coding agents are welcome. See `AGENTS.md` for the specific
operating instructions. Pull requests authored with agent assistance
should be tagged `ai-assisted` in the description; this is not a
gatekeeping flag, just a signal for reviewers.

## 9. Reporting Issues

When reporting a bug:

- Include a minimal reproduction as Python code, not a description.
- State the Python version and the installed pydantic version.
- Paste the full traceback, including the `detail` dict on any
  `LaurenError` subclass.
- If the bug is about decoration order, include the decorator stack
  as it appeared in your source.

## 10. Code of Conduct

Be kind and specific. Review comments should point at a line, a
reason, and a suggested change. "This is wrong" without elaboration
is not a review comment.
