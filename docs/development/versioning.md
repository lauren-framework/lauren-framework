# Versioning

`lauren` uses [`setuptools-scm`](https://setuptools-scm.readthedocs.io/)
to derive the package version directly from git tags.  There is no
`version = "..."` string anywhere in the codebase — the tag *is* the
version.

---

## How It Works

`setuptools-scm` inspects the git repository at build time:

1. If `HEAD` points to an annotated tag matching `vX.Y.Z`, the version
   is `X.Y.Z`.
2. If there are commits after the latest tag, a `post` suffix is
   appended: `X.Y.Z.postN+gHASH`.
3. If the working tree is dirty (uncommitted changes), a `+d` suffix is
   added: `X.Y.Z.postN+gHASH.dYYYYMMDD`.
4. If no tag exists at all (fresh clone without tags), the fallback
   is `0.0.0+unknown`.

The resolved version is written into the installed package's metadata
so that `importlib.metadata.version("lauren")` always returns the
correct string at runtime.

---

## At Runtime

`lauren/__init__.py` exposes `__version__`:

```python
import lauren
print(lauren.__version__)   # e.g. "1.2.3"
```

The implementation uses `importlib.metadata` with a safe fallback:

```python
try:
    from importlib.metadata import version
    __version__ = version("lauren")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
```

The fallback fires only in rare edge cases (e.g., running from a raw
checkout without installing the package).  In all normal workflows
(`pip install -e .`, `pip install lauren`) the metadata is present.

---

## Tagging Conventions

| Tag format | Used for |
|------------|----------|
| `v1.2.3` | Stable release |
| `v1.2.3rc1` | Release candidate |
| `v1.2.3b1` | Beta |
| `v1.2.3a1` | Alpha |

PEP 440 pre-release identifiers are appended directly to the numeric
version, e.g. `1.2.3rc1`.

### Deriving the Next Tag

Use the built-in nox helpers to inspect the latest semantic-version tag and
print the next annotated tag command before you cut a release:

```bash
# Default patch bump
nox -s ver_inc

# Explicit semantic bump
nox -s ver_inc -- --minor
nox -s ver_inc -- --major

# Inspect the previous version if you need to undo a proposal
nox -s ver_dec -- --minor
```

Each session prints the latest `vX.Y.Z` tag it found, the proposed next or
previous tag, and a copy/paste-ready command such as:

```bash
git tag -a v1.2.3 -m "Release v1.2.3"
git push origin v1.2.3
```

### Creating a Tag

```bash
# Annotated tag (recommended — becomes the GitHub Release description):
git tag -a v1.2.3 -m "Release v1.2.3"
git push origin v1.2.3
```

### Listing Tags

```bash
git tag -l "v*" | sort -V
```

### Deleting a Mistaken Tag (before pushing)

```bash
git tag -d v1.2.3
```

If the tag has already been pushed, coordinate with the team before
deleting it remotely — PyPI will reject re-uploads of the same version.

---

## `pyproject.toml` Configuration

```toml
[build-system]
requires = ["setuptools>=61.0", "setuptools-scm>=8.0"]
build-backend = "setuptools.build_meta"

[project]
dynamic = ["version"]

[tool.setuptools_scm]
fallback_version = "0.0.0+unknown"
version_scheme = "post-release"
local_scheme = "dirty-tag"
```

`version_scheme = "post-release"` uses the `X.Y.Z.postN` format for
commits after a tag.  `local_scheme = "dirty-tag"` appends a date
suffix when there are uncommitted changes.
