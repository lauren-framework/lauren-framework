"""Core inspection commands honor streams, exit codes and output formats."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lauren import LaurenFactory, StartupError, controller, get, module
from lauren._commands import _inspect
from lauren._commands._runner import _TargetError, main


@controller("/sample")
class _Controller:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[_Controller])
class _Module:
    pass


@pytest.fixture
def loaded(monkeypatch: pytest.MonkeyPatch) -> object:
    app = LaurenFactory.create(_Module)
    loader = AsyncMock(return_value=SimpleNamespace(app=app))
    monkeypatch.setattr(_inspect, "load_context", loader)
    return app


@pytest.mark.parametrize("format", ["text", "json", "github"])
def test_check_success_never_enters_lifecycle(loaded: object, format: str) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(["check", "sample:app", "--format", format], stdout=out, stderr=err)
    assert code == 0
    assert not err.getvalue()
    assert not loaded._started  # type: ignore[attr-defined]
    if format == "json":
        assert json.loads(out.getvalue())["ok"] is True
    else:
        assert "OK" in out.getvalue()


@pytest.mark.parametrize("format", ["text", "json", "github"])
def test_startup_failure_preserves_code_and_hints(monkeypatch: pytest.MonkeyPatch, format: str) -> None:
    error = StartupError("bad graph", detail={"hints": ["export the token"]})
    monkeypatch.setattr(_inspect, "load_context", AsyncMock(side_effect=error))
    out, err = io.StringIO(), io.StringIO()
    code = main(["check", "sample:app", "--format", format], stdout=out, stderr=err)
    assert code == 1
    assert "export the token" in out.getvalue() + err.getvalue()


@pytest.mark.parametrize(
    "error", [_TargetError("bad target"), ImportError("missing module"), RuntimeError("broken")]
)
def test_loader_errors_map_to_documented_exit_codes(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(_inspect, "load_context", AsyncMock(side_effect=error))
    out, err = io.StringIO(), io.StringIO()
    code = main(["check", "sample:app"], stdout=out, stderr=err)
    assert code == (1 if isinstance(error, RuntimeError) else 2)
    assert err.getvalue()


@pytest.mark.parametrize(
    "flags,expected",
    [
        ([], True),
        (["--with-chain"], True),
        (["--module", "absent"], False),
        (["--method", "POST"], False),
        (["--path", "absent"], False),
    ],
)
def test_routes_filters_and_policy_display(loaded: object, flags: list[str], expected: bool) -> None:
    out = io.StringIO()
    assert main(["routes", "sample:app", *flags], stdout=out, stderr=io.StringIO()) == 0
    assert ("/sample" in out.getvalue()) is expected


def test_openapi_json_to_stdout(loaded: object) -> None:
    out = io.StringIO()
    assert main(["openapi", "sample:app"], stdout=out, stderr=io.StringIO()) == 0
    assert "/sample" in json.loads(out.getvalue())["paths"]


def test_parser_uses_the_supplied_output_stream() -> None:
    out, err = io.StringIO(), io.StringIO()
    assert main(["check", "--help"], stdout=out, stderr=err) == 0
    assert "usage:" in out.getvalue()
    assert not err.getvalue()
    assert main(["check", "--invalid"], stdout=out, stderr=err) == 2
    assert "error:" in err.getvalue()


def test_environment_target_fallback(loaded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUREN_TARGET", "sample:app")
    assert main(["check"], stdout=io.StringIO(), stderr=io.StringIO()) == 0


def test_github_escaping_cannot_inject_another_workflow_command() -> None:
    assert _inspect._github_escape("x\n::error::oops") == "x%0A::error::oops"
    assert _inspect._github_escape("a,b:c%", property_value=True) == "a%2Cb%3Ac%25"


def test_missing_target_reports_usage_in_supplied_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAUREN_TARGET", raising=False)
    err = io.StringIO()
    assert main(["check"], stdout=io.StringIO(), stderr=err) == 2
    assert "missing TARGET" in err.getvalue()


def test_openapi_export_file_is_utf8(loaded: object, tmp_path) -> None:
    output = tmp_path / "spec.json"
    out = io.StringIO()
    assert (
        main(["openapi", "export", "sample:app", "--out", str(output)], stdout=out, stderr=io.StringIO()) == 0
    )
    assert "/sample" in json.loads(output.read_text(encoding="utf-8"))["paths"]
    assert "wrote" in out.getvalue()


@pytest.mark.parametrize("outside", [False, True])
def test_github_diagnostics_render_source_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path, outside: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path.parent / "external.py" if outside else tmp_path / "local.py"
    error = StartupError("bad", detail={"source": {"file": str(source), "line": 12}, "hints": ["fix"]})
    out = io.StringIO()
    assert _inspect._report_failure(error, "check", "github", out, io.StringIO()) == 1
    assert (str(source) if outside else "local.py") in out.getvalue()
    assert "line=12" in out.getvalue()


def test_parser_nonnumeric_exit_code_is_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Parser:
        def parse_args(self, tokens):
            raise SystemExit("bad usage")

    monkeypatch.setattr(_inspect, "_parser", lambda action: Parser())
    assert _inspect.inspect_main("check", [], io.StringIO(), io.StringIO()) == 2


def test_routes_json_uses_compiled_manifest(loaded: object) -> None:
    out = io.StringIO()
    assert main(["routes", "--json", "sample:app"], stdout=out, stderr=io.StringIO()) == 0
    assert any(route["path"] == "/sample" for route in json.loads(out.getvalue()))
