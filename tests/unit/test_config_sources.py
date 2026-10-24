"""Unit tests for configuration sources and the stdlib ``.env`` parser."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from lauren._config._sources import (
    DotenvSyntaxError,
    SourceProblem,
    fill_async_layer,
    is_async_source,
    load_layer,
    parse_dotenv,
    source_label,
)
from lauren.config import AsyncSource, DotenvSource, EnvSource, SecretsDirSource, TomlSource, YamlSource

# ---------------------------------------------------------------------------
# .env parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("KEY=value", {"KEY": "value"}),
        ("KEY = value  ", {"KEY": "value"}),
        ("export KEY=value", {"KEY": "value"}),
        ("export\tKEY=value", {"KEY": "value"}),
        ("KEY=", {"KEY": ""}),
        ("KEY='single quoted # not a comment'", {"KEY": "single quoted # not a comment"}),
        ("KEY='no \\n escapes'", {"KEY": "no \\n escapes"}),
        ('KEY="line\\nbreak\\ttab \\"q\\" \\\\ \\$"', {"KEY": 'line\nbreak\ttab "q" \\ $'}),
        ('KEY="unknown \\x escape"', {"KEY": "unknown \\x escape"}),
        ("KEY=value # trailing comment", {"KEY": "value"}),
        ("KEY=value#not-a-comment", {"KEY": "value#not-a-comment"}),
        ("KEY=#starts-with-hash", {"KEY": "#starts-with-hash"}),
        ("KEY= # only a comment", {"KEY": ""}),
        ('KEY="quoted" # comment', {"KEY": "quoted"}),
        ("# a comment\n\n   \nKEY=1", {"KEY": "1"}),
        ("KEY=1\nKEY=2", {"KEY": "2"}),
        ("A=1\r\nB=2\r\n", {"A": "1", "B": "2"}),
        ("A=1\rB=2", {"A": "1", "B": "2"}),
        ('KEY="first\nsecond"', {"KEY": "first\nsecond"}),
        ("KEY='a\nb\nc'", {"KEY": "a\nb\nc"}),
        ("﻿KEY=bom", {"KEY": "bom"}),
        ("dotted.name-1=x", {"dotted.name-1": "x"}),
        ("URL=postgres://u:p@h/db?x=1", {"URL": "postgres://u:p@h/db?x=1"}),
    ],
)
def test_parse_dotenv(text: str, expected: dict[str, str]) -> None:
    assert parse_dotenv(text) == expected


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("JUSTAKEY", 1),
        ("A=1\nnot a pair", 2),
        ("1KEY=x", 1),
        ("BAD KEY=x", 1),
        ('A=1\nKEY="never closed', 2),
        ("KEY='open\nstill open", 1),
        ('KEY="closed" trailing', 1),
        ('A=1\nKEY="multi\nline" junk', 3),
    ],
)
def test_parse_dotenv_reports_syntax_errors_with_line_numbers(text: str, line: int) -> None:
    with pytest.raises(DotenvSyntaxError) as excinfo:
        parse_dotenv(text)

    assert excinfo.value.line == line
    assert excinfo.value.message.startswith(f"line {line}:")


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------


def test_env_source_reads_the_injected_mapping() -> None:
    layer = load_layer(EnvSource(), profile="test", environ={"A": "1"})

    assert layer.label == "environment"
    assert layer.lookup is not None and layer.lookup("A") == "1"


def test_env_source_reads_os_environ_without_a_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUREN_F12_PROBE", "on")

    layer = load_layer(EnvSource(), profile="test", environ=None)

    assert layer.lookup is not None and layer.lookup("LAUREN_F12_PROBE") == "on"


def test_dotenv_source_substitutes_the_profile(tmp_path: Path) -> None:
    (tmp_path / ".env.production").write_text("A=prod\n", encoding="utf-8")

    layer = load_layer(DotenvSource(tmp_path / ".env.{profile}"), profile="production", environ=None)

    assert layer.label.endswith(".env.production")
    assert layer.lookup is not None and layer.lookup("A") == "prod"


def test_missing_optional_dotenv_file_is_an_empty_unread_layer(tmp_path: Path) -> None:
    layer = load_layer(DotenvSource(tmp_path / ".env", required=False), profile="test", environ=None)

    assert layer.read is False
    assert layer.lookup is not None and layer.lookup("A") is None


@pytest.mark.parametrize("source_cls", [DotenvSource, TomlSource, YamlSource])
def test_missing_required_file_names_the_path(tmp_path: Path, source_cls: type) -> None:
    path = tmp_path / "absent.cfg"

    with pytest.raises(SourceProblem) as excinfo:
        load_layer(source_cls(path), profile="test", environ=None)

    assert excinfo.value.entry["reason"] == "missing_file"
    assert excinfo.value.entry["source"] == str(path)


def test_dotenv_parse_error_becomes_a_problem_with_line(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\noops\n", encoding="utf-8")

    with pytest.raises(SourceProblem) as excinfo:
        load_layer(DotenvSource(path), profile="test", environ=None)

    assert excinfo.value.entry["reason"] == "parse_error"
    assert excinfo.value.entry["line"] == 2


def test_dotenv_undecodable_file_is_a_source_error(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"A=\xff\xfe\n")

    with pytest.raises(SourceProblem) as excinfo:
        load_layer(DotenvSource(path), profile="test", environ=None)

    assert excinfo.value.entry["reason"] == "source_error"


def test_toml_source_produces_a_nested_tree(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('debug = true\n[database]\nurl = "sqlite://"\n', encoding="utf-8")

    layer = load_layer(TomlSource(path), profile="test", environ=None)

    assert layer.kind == "nested"
    assert layer.tree == {"debug": True, "database": {"url": "sqlite://"}}


def test_toml_parse_error_is_a_problem(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("debug = = true\n", encoding="utf-8")

    with pytest.raises(SourceProblem) as excinfo:
        load_layer(TomlSource(path), profile="test", environ=None)

    assert excinfo.value.entry["reason"] == "parse_error"


def test_missing_optional_toml_file_is_an_empty_tree(tmp_path: Path) -> None:
    layer = load_layer(TomlSource(tmp_path / "x.toml", required=False), profile="test", environ=None)

    assert layer.tree == {} and layer.read is False


def test_yaml_source_without_pyyaml_reports_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("debug: true\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "yaml", None)

    with pytest.raises(SourceProblem) as excinfo:
        load_layer(YamlSource(path), profile="test", environ=None)

    assert excinfo.value.entry["reason"] == "source_error"
    assert "lauren[yaml]" in excinfo.value.entry["message"]


class _FakeYamlError(Exception):
    pass


class _FakeYaml:
    """A stand-in for PyYAML so the YAML branch runs without the dependency."""

    YAMLError = _FakeYamlError

    def __init__(self, result: Any) -> None:
        self._result = result

    def safe_load(self, text: str) -> Any:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.mark.parametrize(
    ("result", "expected"),
    [({"debug": True}, {"debug": True}), (None, {})],
)
def test_yaml_source_produces_a_nested_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: Any, expected: dict[str, Any]
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("ignored", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "yaml", _FakeYaml(result))

    layer = load_layer(YamlSource(path), profile="test", environ=None)

    assert layer.tree == expected


@pytest.mark.parametrize("result", [_FakeYamlError("bad"), ["a", "list"]])
def test_yaml_source_rejects_malformed_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: Any
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("ignored", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "yaml", _FakeYaml(result))

    with pytest.raises(SourceProblem) as excinfo:
        load_layer(YamlSource(path), profile="test", environ=None)

    assert excinfo.value.entry["reason"] == "parse_error"


def test_yaml_source_with_real_pyyaml(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    path = tmp_path / "settings.yaml"
    path.write_text("database:\n  url: sqlite://\n", encoding="utf-8")

    layer = load_layer(YamlSource(path), profile="test", environ=None)

    assert layer.tree == {"database": {"url": "sqlite://"}}


def test_secrets_dir_reads_one_file_per_key(tmp_path: Path) -> None:
    (tmp_path / "APP_TOKEN").write_text("tok\n", encoding="utf-8")
    (tmp_path / "app_password").write_text("pw\r\n", encoding="utf-8")

    layer = load_layer(SecretsDirSource(tmp_path), profile="test", environ=None)

    assert layer.lookup is not None
    assert layer.lookup("APP_TOKEN") == "tok"
    assert layer.lookup("APP_PASSWORD") == "pw"
    assert layer.lookup("APP_OTHER") is None


@pytest.mark.parametrize("name", ["", "..", ".", "a/b", "a\\b"])
def test_secrets_dir_ignores_path_like_names(tmp_path: Path, name: str) -> None:
    layer = load_layer(SecretsDirSource(tmp_path), profile="test", environ=None)

    assert layer.lookup is not None and layer.lookup(name) is None


def test_missing_required_secrets_dir_is_a_problem(tmp_path: Path) -> None:
    with pytest.raises(SourceProblem) as excinfo:
        load_layer(SecretsDirSource(tmp_path / "secrets"), profile="test", environ=None)

    assert excinfo.value.entry["reason"] == "missing_file"


def test_missing_optional_secrets_dir_is_empty(tmp_path: Path) -> None:
    layer = load_layer(SecretsDirSource(tmp_path / "secrets", required=False), profile="test", environ=None)

    assert layer.read is False and layer.lookup is not None and layer.lookup("A") is None


def test_secrets_path_that_is_a_file_is_a_problem(tmp_path: Path) -> None:
    path = tmp_path / "secrets"
    path.write_text("", encoding="utf-8")

    with pytest.raises(SourceProblem) as excinfo:
        load_layer(SecretsDirSource(path, required=False), profile="test", environ=None)

    assert excinfo.value.entry["reason"] == "source_error"


# ---------------------------------------------------------------------------
# Async sources
# ---------------------------------------------------------------------------


class _Vault:
    name = "vault"

    def __init__(self, result: Any) -> None:
        self.result = result
        self.keys: list[str] = []

    async def load(self, keys: Any) -> Any:
        self.keys = list(keys)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_async_source_protocol_detection() -> None:
    vault = _Vault({})

    assert isinstance(vault, AsyncSource)
    assert is_async_source(vault)
    assert not is_async_source(EnvSource())
    assert not is_async_source(object())


def test_async_layer_is_pending_until_filled() -> None:
    vault = _Vault({"A": "1"})
    layer = load_layer(vault, profile="test", environ=None)

    assert layer.pending and layer.label == "vault"
    asyncio.run(fill_async_layer(layer, ["A", "B"]))

    assert not layer.pending
    assert layer.lookup is not None and layer.lookup("A") == "1"
    assert vault.keys == ["A", "B"]


@pytest.mark.parametrize("result", [RuntimeError("down"), ["not", "a", "mapping"], {"A": 1}])
def test_async_source_failures_become_problems(result: Any) -> None:
    layer = load_layer(_Vault(result), profile="test", environ=None)

    with pytest.raises(SourceProblem) as excinfo:
        asyncio.run(fill_async_layer(layer, ["A"]))

    assert excinfo.value.entry["reason"] == "source_error"
    assert excinfo.value.entry["source"] == "vault"


def test_source_label_falls_back_to_the_class_name() -> None:
    class Remote:
        async def load(self, keys: Any) -> dict[str, str]:
            return {}

    assert source_label(Remote(), "test") == "Remote"


# ---------------------------------------------------------------------------
# Source objects
# ---------------------------------------------------------------------------


def test_file_sources_compare_by_value() -> None:
    assert DotenvSource(".env") == DotenvSource(".env")
    assert DotenvSource(".env") != DotenvSource(".env", required=False)
    assert DotenvSource(".env") != TomlSource(".env")
    assert hash(DotenvSource(".env")) == hash(DotenvSource(".env"))
    assert EnvSource() == EnvSource() and hash(EnvSource()) == hash(EnvSource())
    assert repr(EnvSource()) == "EnvSource()"
    assert repr(TomlSource("a.toml")) == "TomlSource('a.toml', required=True)"


def test_file_source_accepts_path_objects() -> None:
    assert DotenvSource(Path("conf") / ".env").path == os.path.join("conf", ".env")


def test_file_source_rejects_non_paths() -> None:
    with pytest.raises(TypeError):
        DotenvSource(42)  # type: ignore[arg-type]
