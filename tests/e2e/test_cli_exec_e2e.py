"""End-to-end: ``python -m lauren`` in a subprocess (PRD F19 §10 E2E).

Each test runs the real interpreter on ``tests.e2e.cmd_app`` and checks exit
codes, output, and — through a marker file — that teardown ran.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import lauren

ROOT = Path(__file__).resolve().parents[2]
TARGET = "tests.e2e.cmd_app:AppModule"
TIMEOUT = 120  # generous: the CI machines can be slow to start an interpreter


def _env(marker: Path) -> dict[str, str]:
    package_root = str(Path(lauren.__file__).resolve().parents[1])
    paths = [package_root, str(ROOT), os.environ.get("PYTHONPATH", "")]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in paths if p), "CMD_APP_MARKER": str(marker)}
    env.pop("LAUREN_TARGET", None)
    return env


def _run(marker: Path, *argv: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "lauren", *argv],
        cwd=ROOT,
        env=_env(marker),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=TIMEOUT,
    )


def _events(marker: Path) -> list[str]:
    return marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []


def test_exec_runs_the_command_and_tears_down(tmp_path: Path) -> None:
    marker = tmp_path / "events.txt"

    proc = _run(marker, "exec", TARGET, "db:seed", "--count", "3")

    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "seeded 3 rows\n", "")
    assert _events(marker) == ["post_construct", "session.aclose", "pre_destruct"]


def test_a_failing_command_exits_1(tmp_path: Path) -> None:
    marker = tmp_path / "events.txt"

    proc = _run(marker, "exec", TARGET, "db:fail")

    assert proc.returncode == 1
    assert "RuntimeError: database is gone" in proc.stderr
    assert _events(marker)[-1] == "pre_destruct"


def test_an_invalid_argument_exits_2_without_starting(tmp_path: Path) -> None:
    marker = tmp_path / "events.txt"

    proc = _run(marker, "exec", TARGET, "db:seed", "--count", "many")

    assert proc.returncode == 2
    assert "usage: db:seed" in proc.stderr
    assert _events(marker) == []


def test_a_broken_graph_exits_3_with_the_rendered_error(tmp_path: Path) -> None:
    proc = _run(tmp_path / "events.txt", "exec", "tests.e2e.cmd_app:BrokenModule", "db:seed")

    assert proc.returncode == 3
    assert "error: the application failed to build" in proc.stderr
    assert "MissingProviderError" in proc.stderr


def test_commands_lists_the_commands(tmp_path: Path) -> None:
    proc = _run(tmp_path / "events.txt", "commands", TARGET)

    assert proc.returncode == 0
    assert "db:seed  Insert fixture data." in proc.stdout


def test_console_evaluates_input_with_the_app_started(tmp_path: Path) -> None:
    marker = tmp_path / "events.txt"
    script = "from tests.e2e.cmd_app import Database\nprint(type(get(Database)).__name__)\n"

    proc = _run(marker, "console", TARGET, stdin=script)

    assert proc.returncode == 0
    assert "Database" in proc.stdout
    assert _events(marker) == ["post_construct", "pre_destruct"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_sigint_cancels_the_command_and_exits_130_after_teardown(tmp_path: Path) -> None:
    marker = tmp_path / "events.txt"
    proc = subprocess.Popen(
        [sys.executable, "-m", "lauren", "exec", TARGET, "db:wait"],
        cwd=ROOT,
        env=_env(marker),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline() == "sleeping\n"
        proc.send_signal(signal.SIGINT)
        _, stderr = proc.communicate(timeout=TIMEOUT)
    finally:
        if proc.poll() is None:  # pragma: no cover - only on failure
            proc.kill()
            proc.communicate()

    assert proc.returncode == 130
    assert "interrupted by SIGINT" in stderr
    assert _events(marker) == ["post_construct", "finally", "session.aclose", "pre_destruct"]
