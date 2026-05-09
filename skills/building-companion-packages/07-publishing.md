# 07 — Publishing

## Semantic versioning

Companion packages follow [SemVer](https://semver.org/).  The version is
derived automatically from git tags via `setuptools-scm`:

| Tag | Result on PyPI |
|---|---|
| `v1.0.0` | `1.0.0` |
| `v1.0.0` + 3 commits | `1.0.0.post3.dev0+gABCDEF` (dev build) |
| dirty working tree | `1.0.0.post3.dev0+gABCDEF.d20260510` |

Never set `version =` in `pyproject.toml` — let `setuptools-scm` handle it.

## Creating a release

```bash
# 1. Update CHANGELOG.md — move [Unreleased] entries to [X.Y.Z] section
# 2. Commit
git add CHANGELOG.md && git commit -m "chore: release vX.Y.Z"

# 3. Tag
git tag vX.Y.Z
git push origin main --tags

# 4. GitHub Actions builds and publishes automatically
```

The `release.yml` workflow:
1. Runs `nox -s llms_check` to verify public API docs are in sync
2. Runs `nox -s build` → `nox -s build_check`
3. Publishes to PyPI using OIDC Trusted Publishing (no API tokens needed)
4. Creates a GitHub Release with auto-generated notes

## Setting up PyPI Trusted Publishing

One-time setup in the PyPI dashboard:

1. Go to your PyPI project → **Manage** → **Publishing**
2. Add a **Trusted Publisher**:
   - Publisher: **GitHub Actions**
   - Owner: `lauren-framework`
   - Repository: `lauren-cache`
   - Workflow: `release.yml`
   - Environment: `pypi`
3. In GitHub, create the `pypi` Environment (**Settings → Environments**)
4. Add a required reviewer or deployment protection rules if desired

For TestPyPI, repeat with Environment name `testpypi`.

With OIDC Trusted Publishing there are **no long-lived API tokens**.  The
GitHub Actions OIDC token is exchanged for a short-lived PyPI upload token
at publish time.

## Manual publish to TestPyPI

```bash
# Via GitHub Actions UI: Actions → release → Run workflow → testpypi
# Or locally:
nox -s build build_check
nox -s release_test -- --yes   # uploads to TestPyPI
```

Verify the TestPyPI install:

```bash
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            lauren-cache==X.Y.Z
python -c "from lauren_cache import CacheModule; print('OK')"
```

## `nox -s build` + `nox -s build_check`

```python
@nox.session(python=PRIMARY_PYTHON)
def build(session):
    _clean_build_artifacts()
    session.install("build>=1.2")
    session.run("python", "-m", "build")

@nox.session(python=PRIMARY_PYTHON, name="build_check")
def build_check(session):
    if not DIST_DIR.exists() or not any(DIST_DIR.iterdir()):
        session.error("dist/ is empty — run `nox -s build` first.")
    session.install("twine>=5.1")
    session.run("twine", "check", *[str(p) for p in DIST_DIR.iterdir()])
```

## GitHub Release

The `release.yml` workflow calls `softprops/action-gh-release@v2` with
`generate_release_notes: true`, which creates a Release from the tag with
auto-generated notes (PR titles + authors since last tag).

Customise the notes template in `.github/release.yml`:

```yaml
changelog:
  categories:
    - title: "New features"
      labels: [enhancement]
    - title: "Bug fixes"
      labels: [bug]
    - title: "Documentation"
      labels: [documentation]
```

## `skills/` distribution

Companion packages should ship a `skills/` directory so AI agents can run:

```bash
npx skills add lauren-framework/lauren-cache
```

The `skills/` directory contains `.md` files (one per skill topic) that agents
copy to their global skills directories (`~/.claude/skills/` etc.).

Structure mirrors `lauren-framework/skills/`:

```
skills/
├── README.md               ← index + table
└── building-with-cache/
    ├── SKILL.md
    ├── quickstart.md
    └── advanced-patterns.md
```

Add the `/skills` redirect to `next.config.ts` if you have a companion website.
