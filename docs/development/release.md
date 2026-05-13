# Release Guide

This page walks you through the full release cycle for `lauren`:
preparing a release branch, tagging, building, and publishing to PyPI.

---

## Prerequisites

- You have push access to the `main` branch (or can open a PR to it).
- The `LAUREN_PYPI_TOKEN` secret is configured in the repository
  settings (used by the GitHub Actions `release` workflow).
- Your local environment has Python 3.11+ and the dev dependencies
  installed (`pip install -e ".[dev]"`).

---

## 1. Prepare the Release

### 1.1 Branch from `main`

```bash
git checkout main
git pull origin main
git checkout -b release/v1.2.3
```

Use the version you are about to publish as the branch name — it makes
pull-request titles self-documenting.

### 1.1.1 Pick the Next Version with Nox

You do not edit a version string in source control. Instead, derive the
next semantic version from the latest `vX.Y.Z` git tag:

```bash
# patch bump (default)
nox -s ver_inc

# explicit minor / major bump
nox -s ver_inc -- --minor
nox -s ver_inc -- --major

# inspect the previous version if you need to back up a proposal
nox -s ver_dec -- --minor
```

The session prints:

- The latest release tag it found.
- The proposed next tag.
- A copy/paste-ready annotated tag command, for example:

```bash
git tag -a v1.3.0 -m "Release v1.3.0"
git push origin v1.3.0
```

Use that proposed version in the release branch name:

```bash
git checkout -b release/v1.3.0
```

### 1.2 Update `CHANGELOG.md`

`lauren` follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. Move items from `[Unreleased]` to a new `[1.2.3] - YYYY-MM-DD`
section. Example:

```markdown
## [1.2.3] - 2026-05-01

### Added
- Unified `ExtractionMarker.extract` signature with `ExecutionContext`.
- `@public` decorator and `IS_PUBLIC_KEY` for cooperative guard bypass.

### Fixed
- `LaurenFactory.create()` no longer requires `await`.

### Changed
- `_ExtractionMarker` renamed to `ExtractionMarker` (public alias kept).
```

### 1.3 Run the Full CI Matrix Locally

```bash
nox                       # lint + tests + typecheck (default sessions)
nox -s docs               # strict MkDocs build
nox -s llms_check         # every public symbol is in llms-full.txt
```

All sessions must be green before you open a PR.

### 1.4 Open and Merge the Release PR

Push the branch and open a pull request against `main`. The PR title
should be `release: vX.Y.Z`. After review, merge with a standard merge
commit (no squash — the tag must point to a commit that is already on
`main`).

---

## 2. Tag the Release

After the release PR lands on `main`, derive the final tag and create it:

```bash
git checkout main
git pull origin main

# Optional: re-check the next version from the latest tag history.
nox -s ver_inc -- --minor

# Annotated tag — the message becomes the release description on GitHub.
git tag -a v1.2.3 -m "Release v1.2.3"
```

If you already ran `ver_inc`, you can copy the exact `git tag -a ...`
command from its output instead of typing it manually.

### Push the Tag

```bash
git push origin v1.2.3
```

Pushing a `v*` tag triggers `.github/workflows/release.yml`, which:

1. Runs `nox -s build` to produce `dist/`.
2. Publishes to PyPI via OIDC Trusted Publishing (no token required in
   the environment).

!!! warning "Do not push a tag to a non-`main` commit"
    The release workflow publishes whatever commit the tag points to.
    Always tag the merged `main` commit, not the release branch tip.

---

## 3. Verify the Release

Once the GitHub Actions `release` job completes:

```bash
# In a clean environment:
pip install lauren==1.2.3
python -c "import lauren; print(lauren.__version__)"
# Expected: 1.2.3
```

Then create a GitHub Release:

1. Go to **Releases → Draft a new release**.
2. Select the `v1.2.3` tag.
3. Paste the changelog section as the release body.
4. Publish.

---

## 4. Manual / Local Release (Fallback)

Prefer the GitHub Actions workflow. Use the manual path only if CI is
unavailable:

```bash
nox -s build              # produces dist/lauren-X.Y.Z*.whl + .tar.gz
nox -s build_check        # validates with twine check

# TestPyPI first:
nox -s release_test

# Real PyPI (destructive — cannot be undone):
nox -s release -- --yes
```

!!! danger "Manual releases bypass OIDC"
    The `release` session uses `twine upload`, which requires
    `TWINE_USERNAME=__token__` and `TWINE_PASSWORD=<pypi-token>` in
    your shell environment.

---

## 5. Post-Release Housekeeping

- Add a new `[Unreleased]` section to the top of `CHANGELOG.md`.
- Close the milestone on GitHub (if one was used).
- Announce in the project discussion board or release notes.

---

## Version Format

`lauren` uses **`setuptools-scm`** to derive the package version
from git tags automatically:

| Scenario | Example version |
|----------|-----------------|
| Exactly on a tag | `1.2.3` |
| Commits after a tag | `1.2.3.post4+g1a2b3c4` |
| Untagged / dirty working tree | `0.0.0+unknown` |

You never edit a version number manually. The tag *is* the version.
See [Versioning](versioning.md) for the full details.
