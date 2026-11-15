"""Canonical lint is read-only and uses the same Ruff version as commit hooks."""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]


class _Session:
    def __init__(self, *args: str) -> None:
        self.posargs = list(args)
        self.installed: list[str] = []
        self.commands: list[tuple[str, ...]] = []

    def install(self, *packages: str) -> None:
        self.installed.extend(packages)

    def run(self, *command: str) -> None:
        self.commands.append(command)


def _run(name: str, *args: str) -> _Session:
    # Load only the trusted session function, without requiring nox inside a
    # runtime/test installation or invoking any of its virtualenv machinery.
    source = ast.parse((_ROOT / "noxfile.py").read_text(encoding="utf-8"))
    fn = copy.deepcopy(
        next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == name)
    )
    fn.decorator_list = []
    namespace = {"nox": SimpleNamespace(Session=_Session)}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "noxfile.py", "exec"), namespace)
    session = _Session(*args)
    namespace[name](session)
    return session


def test_lint_is_read_only_unless_fixes_are_requested() -> None:
    session = _run("lint")

    assert session.commands == [("ruff", "check", "lauren", "tests", "scripts")]
    assert "--fix" not in session.commands[0]


def test_lint_supports_explicit_fixes() -> None:
    session = _run("lint", "--fix")

    assert session.commands == [("ruff", "check", "--fix", "lauren", "tests", "scripts")]


@pytest.mark.parametrize("name", ["lint", "format"])
def test_nox_ruff_matches_the_hook_pin(name: str) -> None:
    config = (_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    pin = re.search(r"ruff-pre-commit\s+rev: v([^\s]+)", config)
    assert pin is not None

    session = _run(name)

    assert session.installed == [f"ruff=={pin[1]}"]
