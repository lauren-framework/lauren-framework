<!--
Thanks for sending a pull request!

Please read CONTRIBUTING.md and AGENTS.md / .CLAUDE.md before opening
this PR — they contain the design invariants reviewers use to accept or
reject changes.
-->

## Summary

<!-- One paragraph describing *what* this PR does and *why*. -->

## Linked issues

<!-- Use "Closes #123" / "Fixes #123" / "Refs #123" syntax. -->

Closes #

## Type of change

- [ ] 🐞 Bug fix (non-breaking change that fixes a defect)
- [ ] ✨ New feature (non-breaking change that adds functionality)
- [ ] 💥 Breaking change (would require a major version bump)
- [ ] 📚 Documentation only
- [ ] 🧹 Refactor / internal cleanup (no behavior change)
- [ ] ⚡ Performance improvement
- [ ] 🧪 Test-only change
- [ ] 🛠️ Build / CI / tooling

## Design checklist

> See [`.CLAUDE.md`](../.CLAUDE.md) — these invariants are *not* optional.

- [ ] **Startup validates; runtime dispatches.** Any new misuse fails inside `LaurenFactory.create(...)`, not on the first request.
- [ ] **Decorators attach metadata; they don't rewrite functions.** No `functools.wraps` mutation of the user's callable.
- [ ] **No reflection on the request path.** All `inspect.signature(...)`, `get_type_hints(...)`, etc. happen at startup.
- [ ] Every public name is exported in `__all__` *and* documented in `llms-full.txt`.
- [ ] Type hints are tight and `mypy --strict` clean for the affected modules.

## Tests

- [ ] I added at least one test that fails on `main` and passes here, OR this PR is test/docs-only.
- [ ] `nox -s tests` passes locally.
- [ ] `nox -s coverage` does not lower coverage on the touched modules.

## Documentation

- [ ] I updated `README.md` if the public surface changed.
- [ ] I updated `llms-full.txt` for any new/changed public symbol.
- [ ] I updated the mkdocs site (`docs/`) if there's a user-facing concept change.

## Reviewer notes

<!--
Anything subtle reviewers should look at first? Tricky concurrency? A
deliberate behavior change that might surprise users?
-->

## Screenshots / logs (optional)

<!-- For perf or DX changes, paste before/after numbers here. -->
