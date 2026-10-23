#!/usr/bin/env python3
"""Docs-as-tests for Lauren's AI-facing documentation (``nox -s docs_contract``).

The reference files are read by coding agents as ground truth, so a code block
that does not compile — or an attribute the code does not have — becomes broken
user code. This script checks, with the standard library only:

1. **Compile** — every fenced ``python`` block in ``lauren/llms-full.txt``,
   ``lauren/llms.txt``, ``README.md``, ``docs/**/*.md`` and ``skills/**/*.md``
   compiles (top-level ``await`` allowed). ``docs/generated-reference/`` is
   generated from the code and skipped.
2. **Run** — blocks marked ``run`` execute, in order, in one module namespace
   per file (later blocks may use names from earlier ones, like a doctest
   session) with ``from __future__ import annotations`` in effect. A top-level
   ``await`` runs under ``asyncio.run``.
3. **Attributes** — every name listed under ``[attributes]`` in
   ``docs_contract.toml`` exists on its class.
4. **Structure** — ``lauren/llms-full.txt`` has exactly one ``_End of
   reference._`` marker, as its last line, a table of contents matching its
   ``##`` headings, and no duplicated trailing block.

Markers. In files MkDocs does not render (``llms*.txt``, ``README.md``,
``skills/``) put the marker in the info string — ```` ```python run ```` or
```` ```python fragment ````. MkDocs pages (``docs/``) cannot carry extra
info-string words, so use an HTML comment on the line before the fence:
``<!-- docs-contract: run -->`` / ``<!-- docs-contract: fragment -->``.
``fragment`` (alias ``skip``) exempts an intentionally partial block from
compilation; blocks made of snippet includes (``--8<--``) and ``>>>`` sessions
are skipped automatically.

Exit codes: 0 — clean; 1 — at least one violation; 2 — configuration error.
"""

from __future__ import annotations

import __future__ as _future
import argparse
import ast
import asyncio
import contextlib
import importlib
import inspect
import io
import re
import sys
import tomllib
import traceback
import types
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "docs_contract.toml"

SOURCE_GLOBS: tuple[str, ...] = (
    "lauren/llms-full.txt",
    "lauren/llms.txt",
    "README.md",
    "docs/**/*.md",
    "skills/**/*.md",
)
EXCLUDED_PREFIXES: tuple[str, ...] = ("docs/generated-reference/",)

PYTHON_LANGUAGES = frozenset({"python", "py", "python3"})
MARKERS = frozenset({"fragment", "skip", "run"})
END_MARKER = "_End of reference._"
#: A ``run`` block with top-level ``await`` that takes longer fails instead of hanging CI.
RUN_TIMEOUT_S = 20.0

_FENCE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_COMMENT_MARKER_RE = re.compile(r"^\s*<!--\s*docs-contract:\s*(?P<marker>[a-z]+)\s*-->\s*$")
_COMPILE_FLAGS = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT | _future.annotations.compiler_flag


@dataclass(frozen=True)
class Block:
    """One fenced code block."""

    path: str
    line: int
    language: str
    markers: frozenset[str]
    source: str

    @property
    def is_python(self) -> bool:
        return self.language in PYTHON_LANGUAGES

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass(frozen=True)
class Violation:
    """A failed check, printed as ``path:line: [kind] message``."""

    location: str
    kind: str
    message: str

    def __str__(self) -> str:
        return f"{self.location}: [{self.kind}] {self.message}"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _strip_indent(line: str, indent: str) -> str:
    return line[len(indent) :] if line.startswith(indent) else line.lstrip(" \t")


def _is_closing_fence(line: str, fence: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= len(fence) and set(stripped) == {fence[0]}


def extract_blocks(text: str, path: str = "<string>") -> list[Block]:
    """Return every fenced code block in *text*, with its markers.

    A marker comment applies to the next fence when only blank lines separate
    them. Info-string words after the language that are known markers count
    too; other words (``title="x.py"``) are ignored.
    """
    lines = text.splitlines()
    blocks: list[Block] = []
    pending: set[str] = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        comment = _COMMENT_MARKER_RE.match(line)
        if comment:
            pending.add(comment["marker"])
            i += 1
            continue
        fence_match = _FENCE_RE.match(line)
        if fence_match is None or "`" in fence_match["info"] and fence_match["fence"][0] == "`":
            if line.strip():
                pending = set()
            i += 1
            continue
        indent, fence = fence_match["indent"], fence_match["fence"]
        words = fence_match["info"].replace("{", " ").replace("}", " ").replace(".", " ").split()
        language = words[0].lower() if words else ""
        markers = pending | {word for word in words[1:] if word in MARKERS}
        body: list[str] = []
        j = i + 1
        while j < len(lines) and not _is_closing_fence(lines[j], fence):
            body.append(_strip_indent(lines[j], indent))
            j += 1
        blocks.append(Block(path, i + 1, language, frozenset(markers), "\n".join(body) + "\n"))
        pending = set()
        i = j + 1
    return blocks


def _auto_skip_reason(block: Block) -> str | None:
    if "--8<--" in block.source:
        return "snippet include"
    first = next((ln.strip() for ln in block.source.splitlines() if ln.strip()), "")
    if first.startswith(">>>"):
        return "interactive session"
    return None


def iter_source_files(root: Path = ROOT, globs: tuple[str, ...] = SOURCE_GLOBS) -> list[Path]:
    """Return the documentation files the contract covers, sorted and de-duplicated."""
    found: set[Path] = set()
    for pattern in globs:
        for path in root.glob(pattern):
            rel = path.relative_to(root).as_posix()
            if path.is_file() and not rel.startswith(EXCLUDED_PREFIXES):
                found.add(path)
    return sorted(found)


# ---------------------------------------------------------------------------
# Compile and run
# ---------------------------------------------------------------------------


def compile_block(block: Block) -> types.CodeType:
    """Compile *block* the way the contract runs it; raises ``SyntaxError``."""
    return compile(block.source, block.location, "exec", flags=_COMPILE_FLAGS, dont_inherit=True)


def check_compile(blocks: list[Block]) -> list[Violation]:
    violations: list[Violation] = []
    for block in blocks:
        if not block.is_python or block.markers & {"fragment", "skip"} or _auto_skip_reason(block):
            continue
        try:
            compile_block(block)
        except SyntaxError as exc:
            line = block.line + (exc.lineno or 0)
            violations.append(
                Violation(f"{block.path}:{line}", "compile", f"{exc.msg} (block at line {block.line})")
            )
    return violations


def run_blocks(blocks: list[Block], module_name: str) -> list[Violation]:
    """Execute the ``run`` blocks of one file in a shared, importable module."""
    runnable = [b for b in blocks if b.is_python and "run" in b.markers]
    if not runnable:
        return []
    module = types.ModuleType(module_name)
    module.__dict__["__file__"] = runnable[0].path
    sys.modules[module_name] = module
    violations: list[Violation] = []
    try:
        for block in runnable:
            output = io.StringIO()
            try:
                # Examples may print or log; keep the report readable and
                # show the captured output only when the block fails.
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    code = compile_block(block)
                    if code.co_flags & inspect.CO_COROUTINE:
                        coroutine = eval(code, module.__dict__)  # noqa: S307 - executing our own docs
                        asyncio.run(asyncio.wait_for(coroutine, RUN_TIMEOUT_S))
                    else:
                        exec(code, module.__dict__)  # noqa: S102 - executing our own docs
            except Exception as exc:  # noqa: BLE001 - every failure is a violation
                detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                captured = output.getvalue().strip()
                if captured:
                    detail += "\n    output: " + captured[-500:].replace("\n", "\n    ")
                violations.append(Violation(block.location, "run", detail))
                break  # later blocks usually depend on this one
    finally:
        sys.modules.pop(module_name, None)
    return violations


# ---------------------------------------------------------------------------
# Attribute claims
# ---------------------------------------------------------------------------


def _resolve(dotted: str) -> object:
    module_name, _, attr = dotted.rpartition(".")
    return getattr(importlib.import_module(module_name), attr)


def has_attribute(obj: object, name: str) -> bool:
    """``hasattr`` that also sees dataclass fields and ``__slots__`` without defaults."""
    if hasattr(obj, name):
        return True
    if name in getattr(obj, "__dataclass_fields__", {}):
        return True
    for klass in getattr(obj, "__mro__", ()):
        slots = klass.__dict__.get("__slots__", ())
        if name in ((slots,) if isinstance(slots, str) else slots):
            return True
    return False


def check_attributes(claims: dict[str, list[str]]) -> list[Violation]:
    violations: list[Violation] = []
    for dotted, names in claims.items():
        try:
            obj = _resolve(dotted)
        except Exception as exc:  # noqa: BLE001
            violations.append(Violation(CONFIG_PATH.name, "attr", f"cannot import {dotted}: {exc}"))
            continue
        for name in names:
            if not has_attribute(obj, name):
                violations.append(Violation(CONFIG_PATH.name, "attr", f"{dotted} has no attribute {name!r}"))
    return violations


# ---------------------------------------------------------------------------
# llms-full.txt structure
# ---------------------------------------------------------------------------


def github_slug(heading: str) -> str:
    """GitHub-style anchor for a heading (what the TOC links point at)."""
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\- ]", "", slug)
    return slug.replace(" ", "-")


def _headings(lines: list[str], level: int) -> list[tuple[int, str]]:
    prefix = "#" * level + " "
    found: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(lines, start=1):
        match = _FENCE_RE.match(line)
        if fence is None and match:
            fence = match["fence"]
            continue
        if fence is not None:
            if _is_closing_fence(line, fence):
                fence = None
            continue
        if line.startswith(prefix):
            found.append((number, line[len(prefix) :].strip()))
    return found


def check_llms_structure(text: str, path: str = "lauren/llms-full.txt") -> list[Violation]:
    """Lint the reference's skeleton: end marker, TOC, duplicated tail."""
    violations: list[Violation] = []
    lines = text.splitlines()

    marker_lines = [n for n, line in enumerate(lines, start=1) if line.strip() == END_MARKER]
    last_content = max((n for n, line in enumerate(lines, start=1) if line.strip()), default=0)
    if len(marker_lines) != 1:
        violations.append(
            Violation(
                path, "structure", f"expected exactly one {END_MARKER!r} line, found {len(marker_lines)}"
            )
        )
    elif marker_lines[0] != last_content:
        violations.append(
            Violation(f"{path}:{marker_lines[0]}", "structure", f"{END_MARKER!r} must be the last line")
        )

    headings = [title for _n, title in _headings(lines, 2) if title != "Table of Contents"]
    toc_start = next((n for n, line in enumerate(lines) if line.strip() == "## Table of Contents"), None)
    if toc_start is None:
        violations.append(Violation(path, "structure", "missing '## Table of Contents'"))
    else:
        entries: list[tuple[str, str]] = []
        for line in lines[toc_start + 1 :]:
            if line.startswith("## ") or line.strip() == "---":
                break
            link = re.match(r"\s*(?:\d+\.|[-*])\s+\[(?P<title>.+)\]\(#(?P<anchor>[^)]+)\)", line)
            if link:
                entries.append((link["title"], link["anchor"]))
        toc_titles = {title for title, _anchor in entries}
        for title in sorted(set(headings) - toc_titles):
            violations.append(
                Violation(path, "structure", f"heading {title!r} is missing from the table of contents")
            )
        for title in sorted(toc_titles - set(headings)):
            violations.append(
                Violation(path, "structure", f"table of contents entry {title!r} has no '## ' heading")
            )
        for title, anchor in entries:
            if title in headings and anchor != github_slug(title):
                violations.append(
                    Violation(
                        path,
                        "structure",
                        f"TOC anchor #{anchor} for {title!r} should be #{github_slug(title)}",
                    )
                )

    content = [line for line in lines if line.strip()]
    for size in range(3, min(40, len(content) // 2) + 1):
        if content[-size:] == content[-2 * size : -size]:
            violations.append(Violation(path, "structure", f"the last {size} non-blank lines are duplicated"))
            break
    return violations


def heading_anchors(text: str) -> set[str]:
    """Anchors of every ATX heading (levels 1-6) outside code fences."""
    lines = text.splitlines()
    return {github_slug(title) for level in range(1, 7) for _n, title in _headings(lines, level)}


def check_reference_links(llms_txt: str, llms_full: str) -> list[Violation]:
    """Every ``llms-full.txt#anchor`` link in llms.txt and ``#anchor`` link in llms-full.txt must resolve."""
    anchors = heading_anchors(llms_full)
    violations: list[Violation] = []
    for anchor in sorted(set(re.findall(r"\]\(llms-full\.txt#([^)]+)\)", llms_txt))):
        if anchor not in anchors:
            violations.append(
                Violation(
                    "lauren/llms.txt", "structure", f"link to llms-full.txt#{anchor} has no matching heading"
                )
            )
    for anchor in sorted(set(re.findall(r"\]\(#([^)]+)\)", llms_full))):
        if anchor not in anchors:
            violations.append(
                Violation("lauren/llms-full.txt", "structure", f"link to #{anchor} has no matching heading")
            )
    return violations


def check_facts() -> list[Violation]:
    """Generated fact regions (``scripts/sync_doc_facts.py``) must be current."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sync_doc_facts  # noqa: PLC0415

    return [
        Violation(rel, "facts", "generated fact regions are stale — run `python scripts/sync_doc_facts.py`")
        for rel in sync_doc_facts.stale_regions()
    ]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def load_config(path: Path = CONFIG_PATH) -> dict[str, object]:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-run", action="store_true", help="compile only; skip executing 'run' blocks")
    parser.add_argument("--stats", action="store_true", help="print block counts per file")
    args = parser.parse_args(argv)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        config = load_config()
    except tomllib.TOMLDecodeError as exc:
        print(f"ERROR: {CONFIG_PATH.name}: {exc}", file=sys.stderr)
        return 2

    violations: list[Violation] = []
    totals = {"python": 0, "compiled": 0, "run": 0}
    for index, file_path in enumerate(iter_source_files()):
        rel = file_path.relative_to(ROOT).as_posix()
        blocks = extract_blocks(file_path.read_text(encoding="utf-8"), rel)
        python_blocks = [b for b in blocks if b.is_python]
        compiled = [
            b for b in python_blocks if not (b.markers & {"fragment", "skip"} or _auto_skip_reason(b))
        ]
        runs = [b for b in python_blocks if "run" in b.markers]
        totals["python"] += len(python_blocks)
        totals["compiled"] += len(compiled)
        totals["run"] += len(runs)
        if args.stats and python_blocks:
            print(f"{rel}: {len(python_blocks)} python, {len(compiled)} compiled, {len(runs)} run")
        violations.extend(check_compile(blocks))
        if not args.no_run:
            violations.extend(run_blocks(blocks, f"_docs_contract_{index}"))

    attributes = config.get("attributes", {})
    if not isinstance(attributes, dict):
        print(f"ERROR: {CONFIG_PATH.name}: [attributes] must be a table", file=sys.stderr)
        return 2
    violations.extend(check_attributes(attributes))
    llms_full = (ROOT / "lauren" / "llms-full.txt").read_text(encoding="utf-8")
    violations.extend(check_llms_structure(llms_full))
    violations.extend(
        check_reference_links((ROOT / "lauren" / "llms.txt").read_text(encoding="utf-8"), llms_full)
    )
    violations.extend(check_facts())

    for violation in violations:
        print(violation)
    summary = (
        f"{totals['python']} python blocks, {totals['compiled']} compiled, "
        f"{0 if args.no_run else totals['run']} run, {sum(len(v) for v in attributes.values())} attribute claims"
    )
    if violations:
        print(f"FAILED: {len(violations)} violation(s) — {summary}")
        return 1
    print(f"OK: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
