#!/usr/bin/env python3
"""Regenerate the fact regions of the docs from the code.

Hard-coded counts and hand-copied tables drift; these regions are written from
``lauren`` itself instead. A region is delimited by HTML comments, which
MkDocs, GitHub and the website all hide::

    <!-- lauren:facts:error-catalog -->
    ...generated...
    <!-- /lauren:facts -->

Available facts:

* ``error-catalog`` — every class in ``lauren.exceptions``: base, HTTP status,
  ``code``, and its meaning (the first paragraph of the class docstring, which
  is the single source of truth).
* ``error-hierarchy`` — the same classes as a tree.

``nox -s docs`` rewrites the regions; ``--check`` (used by ``nox -s
docs_contract``) exits 1 when a region is stale instead of rewriting it.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import re
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS: tuple[str, ...] = ("docs/reference/errors.md", "lauren/llms-full.txt")

_REGION_RE = re.compile(
    r"(?P<open><!-- lauren:facts:(?P<name>[a-z0-9-]+) -->\n)(?P<body>.*?)(?P<close><!-- /lauren:facts -->)",
    re.DOTALL,
)
_ROLE_RE = re.compile(r":[a-z]+:`~?(?:[\w.]+\.)?(?P<name>\w+)`")


def _import_exceptions():  # noqa: ANN202 - module object
    """Return ``lauren.exceptions`` without importing the ``lauren`` package.

    ``nox -s docs`` runs this script in a virtualenv that has the docs tooling
    but not lauren's runtime dependencies (``anyio``, ...), and the module only
    needs the standard library, so load the file directly. An already-imported
    module (as under pytest) is reused so its classes stay the framework's own.
    """
    loaded = sys.modules.get("lauren.exceptions")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location("lauren.exceptions", ROOT / "lauren" / "exceptions.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog() -> list[type]:
    exceptions = _import_exceptions()
    return [
        obj
        for obj in vars(exceptions).values()
        if inspect.isclass(obj) and issubclass(obj, BaseException) and obj.__module__ == exceptions.__name__
    ]


def _meaning(cls: type) -> str:
    """First paragraph of the docstring, as one line of Markdown."""
    doc = inspect.cleandoc(cls.__dict__.get("__doc__") or "")
    paragraph = doc.split("\n\n", 1)[0].replace("\n", " ").strip()
    paragraph = _ROLE_RE.sub(lambda m: f"``{m['name']}``", paragraph)
    return paragraph.replace("``", "`").replace("|", "\\|")


def _status(cls: type) -> str:
    status = getattr(cls, "status_code", None)
    return str(status) if status is not None else "—"


def error_catalog() -> str:
    lines = ["| Class | Base | HTTP status | `code` | Meaning |", "|---|---|---|---|---|"]
    for cls in _catalog():
        base = cls.__bases__[0].__name__
        lines.append(f"| `{cls.__name__}` | `{base}` | {_status(cls)} | `{cls.code}` | {_meaning(cls)} |")
    return "\n".join(lines) + "\n"


def error_hierarchy() -> str:
    classes = _catalog()
    children: dict[type, list[type]] = {cls: [] for cls in classes}
    roots: list[type] = []
    for cls in classes:
        parent = cls.__bases__[0]
        (children[parent] if parent in children else roots).append(cls)

    def label(cls: type) -> str:
        status = getattr(cls, "status_code", None)
        suffix = f"  → {status}  code={cls.code}" if status is not None else ""
        return cls.__name__ + suffix

    out: list[str] = []

    def walk(cls: type, prefix: str, last: bool, depth: int) -> None:
        if depth == 0:
            out.append(label(cls))
        else:
            out.append(f"{prefix}{'└── ' if last else '├── '}{label(cls)}")
        kids = children[cls]
        for index, kid in enumerate(kids):
            extension = "" if depth == 0 else ("    " if last else "│   ")
            walk(kid, prefix + extension, index == len(kids) - 1, depth + 1)

    for root in roots:
        walk(root, "", True, 0)
    return "```text\n" + "\n".join(out) + "\n```\n"


FACTS: dict[str, Callable[[], str]] = {
    "error-catalog": error_catalog,
    "error-hierarchy": error_hierarchy,
}


def render(text: str, path: str = "<string>") -> str:
    """Return *text* with every fact region regenerated."""

    def replace(match: re.Match[str]) -> str:
        name = match["name"]
        if name not in FACTS:
            raise KeyError(f"{path}: unknown fact region {name!r} (known: {', '.join(sorted(FACTS))})")
        return match["open"] + FACTS[name]() + match["close"]

    return _REGION_RE.sub(replace, text)


def stale_regions(root: Path = ROOT, targets: tuple[str, ...] = TARGETS) -> list[str]:
    """Paths of target files whose fact regions are out of date."""
    stale: list[str] = []
    for rel in targets:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        if render(text, rel) != text:
            stale.append(rel)
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if a region is stale; write nothing")
    args = parser.parse_args(argv)

    stale = stale_regions()
    if args.check:
        for rel in stale:
            print(f"{rel}: fact regions are stale — run `python scripts/sync_doc_facts.py`")
        return 1 if stale else 0
    for rel in stale:
        path = ROOT / rel
        path.write_text(render(path.read_text(encoding="utf-8"), rel), encoding="utf-8")
        print(f"updated {rel}")
    if not stale:
        print("doc facts are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
