"""The core inspection commands compile an app without serving or starting it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_APP = """\
from lauren import LaurenFactory, controller, get, module, post_construct, injectable

@injectable()
class Dependency:
    @post_construct
    async def connect(self):
        raise RuntimeError("lifecycle should not run")

@controller("/hello")
class Hello:
    @get("/")
    async def index(self, dep: Dependency) -> dict:
        return {"hello": "world"}

@module(controllers=[Hello], providers=[Dependency])
class AppModule: ...

app = LaurenFactory.create(AppModule)
"""

_BROKEN = """\
from lauren import LaurenFactory, controller, get, module, injectable

@injectable()
class Repository: ...

@controller("/broken")
class Controller:
    @get("/")
    async def handler(self, repo: Repository): ...

@module(providers=[Repository])
class DataModule: ...

@module(imports=[DataModule], controllers=[Controller])
class AppModule: ...

app = LaurenFactory.create(AppModule)
"""


def _run(tmp_path: Path, source: str, *args: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "myapp.py").write_text(source, encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), str(repo_root)])}
    return subprocess.run(
        [sys.executable, "-m", "lauren", *args],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_check_exits_zero_without_running_post_construct(tmp_path: Path) -> None:
    result = _run(tmp_path, _APP, "check", "myapp:app")

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
    assert "lifecycle should not run" not in result.stderr


def test_check_reports_startup_hint_as_github_annotation(tmp_path: Path) -> None:
    result = _run(tmp_path, _BROKEN, "check", "--format", "github", "myapp:app")

    assert result.returncode == 1
    assert "::error file=" in result.stdout
    assert "exports=[Repository]" in result.stdout
    assert "unresolvable_parameter" in result.stdout


def test_check_returns_two_for_an_import_error(tmp_path: Path) -> None:
    result = _run(tmp_path, _APP, "check", "no_such_module:app")

    assert result.returncode == 2
    assert "cannot import" in result.stderr


def test_routes_lists_compiled_http_routes_as_json(tmp_path: Path) -> None:
    result = _run(tmp_path, _APP, "routes", "--json", "myapp:app")

    assert result.returncode == 0, result.stderr
    routes = json.loads(result.stdout)
    assert any(route["path"] == "/hello" and route["method"] == "GET" for route in routes)


def test_openapi_export_writes_document_without_startup(tmp_path: Path) -> None:
    result = _run(tmp_path, _APP, "openapi", "export", "--out", "spec.json", "myapp:app")

    assert result.returncode == 0, result.stderr
    document = json.loads((tmp_path / "spec.json").read_text(encoding="utf-8"))
    assert "/hello" in document["paths"]


def test_check_json_failure_keeps_existing_error_code_and_hints(tmp_path: Path) -> None:
    result = _run(tmp_path, _BROKEN, "check", "--format=json", "myapp:app")

    assert result.returncode == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "unresolvable_parameter"
    assert any("exports=[Repository]" in hint for hint in error["detail"]["hints"])


def test_routes_filter_by_path_and_method(tmp_path: Path) -> None:
    result = _run(tmp_path, _APP, "routes", "--json", "--path", "hello", "--method", "POST", "myapp:app")

    assert result.returncode == 0
    assert json.loads(result.stdout) == []


def test_core_cli_rejects_missing_target_without_importing_anything(tmp_path: Path) -> None:
    result = _run(tmp_path, _APP, "check")

    assert result.returncode == 2
    assert "missing TARGET" in result.stderr
