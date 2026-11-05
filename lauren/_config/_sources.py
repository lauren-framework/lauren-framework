"""Configuration sources: environment, ``.env``, TOML, YAML, secret files.

A source is an immutable description ("read ``.env.{profile}`` if it
exists"); nothing is read when it is constructed. The loader asks each
source for a :class:`_Layer` once per load (once per
``LaurenFactory.create`` call), so two apps with different sources or
``environ=`` mappings never share state, and the process environment is
read but never written.

Two layer shapes exist:

* **flat** — string values keyed by environment-variable name
  (:class:`EnvSource`, :class:`DotenvSource`, :class:`SecretsDirSource`
  and every :class:`AsyncSource`). Values go through lax coercion.
* **nested** — typed values in tables (:class:`TomlSource`,
  :class:`YamlSource`). Values are validated strictly.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

_PROFILE_PLACEHOLDER = "{profile}"


class EnvSource:
    """Read the process environment — or the ``environ=`` mapping, when one is given.

    Always list it **last**: the environment wins over every file, so a
    ``.env`` file can never override a real environment variable.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "EnvSource()"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EnvSource)

    def __hash__(self) -> int:
        return hash(EnvSource)


class _FileSource:
    """Shared shape of the file-backed sources."""

    __slots__ = ("path", "required", "encoding")

    def __init__(
        self, path: str | os.PathLike[str], *, required: bool = True, encoding: str = "utf-8"
    ) -> None:
        if not isinstance(path, (str, os.PathLike)):
            raise TypeError(f"{type(self).__name__}() expects a path, got {type(path).__name__}")
        self.path = os.fspath(path)
        self.required = bool(required)
        self.encoding = encoding

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.path!r}, required={self.required})"

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        assert isinstance(other, _FileSource)
        return (self.path, self.required, self.encoding) == (other.path, other.required, other.encoding)

    def __hash__(self) -> int:
        return hash((type(self), self.path, self.required, self.encoding))


class DotenvSource(_FileSource):
    """Read ``KEY=VALUE`` lines from a ``.env`` file into a private mapping.

    Supports ``export`` prefixes, ``#`` comments, single-quoted (literal)
    and double-quoted (escapes, multi-line) values. ``{profile}`` in the
    path is replaced by the active profile. The file never touches
    ``os.environ``. No ``${VAR}`` interpolation is performed.
    """

    __slots__ = ()


class TomlSource(_FileSource):
    """Read a TOML file (stdlib ``tomllib``); tables map to nested settings sections.

    Values are typed, so they are validated strictly (no string parsing of
    numbers or booleans). ``{profile}`` in the path is substituted.
    """

    __slots__ = ()


class YamlSource(_FileSource):
    """Read a YAML file (requires PyYAML: ``pip install 'lauren[yaml]'``).

    Behaves like :class:`TomlSource`: mappings are nested sections and
    values are validated strictly.
    """

    __slots__ = ()


class SecretsDirSource(_FileSource):
    """Read one file per key from a directory, such as Docker / Kubernetes ``/run/secrets``.

    The file name is the environment-variable name (``APP_DB_PASSWORD``),
    or its lower-case form; trailing newlines are stripped.
    """

    __slots__ = ()


@runtime_checkable
class AsyncSource(Protocol):
    """A source that must be awaited — Vault, AWS Secrets Manager, ...

    ``load`` receives every environment-variable name the settings class
    can read and returns the ones it knows. It runs at the start of
    ``LaurenApp.startup()`` (before ``@post_construct``), or inside
    :func:`~lauren.config.aload_settings`. An optional ``name`` attribute
    labels the source in errors and logs.
    """

    async def load(self, keys: Sequence[str]) -> Mapping[str, str]: ...


def is_async_source(source: Any) -> bool:
    """Return True for an object implementing :class:`AsyncSource`."""
    import inspect

    return not isinstance(source, (EnvSource, _FileSource)) and inspect.iscoroutinefunction(
        getattr(source, "load", None)
    )


def source_label(source: Any, profile: str) -> str:
    """Return the human-readable label used in errors and the startup log."""
    if isinstance(source, EnvSource):
        return "environment"
    if isinstance(source, _FileSource):
        return source.path.replace(_PROFILE_PLACEHOLDER, profile)
    return str(getattr(source, "name", None) or type(source).__name__)


# ---------------------------------------------------------------------------
# Layers — what a source produced during one load
# ---------------------------------------------------------------------------


@dataclass
class _Layer:
    """The data one source contributed to one load."""

    label: str
    #: ``"flat"`` (string values by env name), ``"nested"`` (typed tables)
    #: or ``"async"`` (flat, filled in later by :func:`fill_async_layer`).
    kind: str
    source: Any = None
    lookup: Callable[[str], str | None] | None = None
    tree: Mapping[str, Any] | None = None
    #: False when an optional file did not exist (the layer is empty).
    read: bool = True

    @property
    def pending(self) -> bool:
        return self.kind == "async" and self.lookup is None


class SourceProblem(Exception):
    """A source could not be read; becomes one ``ConfigValidationError`` entry."""

    def __init__(self, entry: dict[str, Any]) -> None:
        super().__init__(entry.get("message", ""))
        self.entry = entry


def _problem(reason: str, label: str, message: str, **extra: Any) -> SourceProblem:
    return SourceProblem({"reason": reason, "source": label, "message": message, **extra})


def load_layer(source: Any, *, profile: str, environ: Mapping[str, str] | None) -> _Layer:
    """Produce the :class:`_Layer` for ``source`` or raise :class:`SourceProblem`."""
    label = source_label(source, profile)
    if isinstance(source, EnvSource):
        env: Mapping[str, str] = environ if environ is not None else os.environ
        return _Layer(label, "flat", source, lookup=env.get)
    if not isinstance(source, _FileSource):
        return _Layer(label, "async", source)
    path = Path(label)
    if isinstance(source, SecretsDirSource):
        if path.exists() and not path.is_dir():
            raise _problem("source_error", label, f"secrets path {label} is not a directory")
        if not path.is_dir():
            if source.required:
                raise _problem("missing_file", label, f"secrets directory {label} does not exist")
            return _Layer(label, "flat", source, lookup=_no_lookup, read=False)
        return _Layer(label, "flat", source, lookup=_secrets_dir_lookup(path, source.encoding))
    if not path.is_file():
        if source.required:
            raise _problem("missing_file", label, f"required file {label} does not exist")
        kind = "flat" if isinstance(source, DotenvSource) else "nested"
        return _Layer(label, kind, source, lookup=_no_lookup, tree={}, read=False)
    if isinstance(source, DotenvSource):
        try:
            values = parse_dotenv(path.read_text(encoding=source.encoding))
        except DotenvSyntaxError as exc:
            raise _problem("parse_error", label, exc.message, line=exc.line) from None
        except (OSError, UnicodeDecodeError) as exc:
            raise _problem("source_error", label, str(exc)) from None
        return _Layer(label, "flat", source, lookup=values.get)
    if isinstance(source, TomlSource):
        try:
            with path.open("rb") as fh:
                tree = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise _problem("parse_error", label, str(exc)) from None
        except OSError as exc:
            raise _problem("source_error", label, str(exc)) from None
        return _Layer(label, "nested", source, tree=tree)
    return _Layer(label, "nested", source, tree=_load_yaml(path, label, source.encoding))


def _load_yaml(path: Path, label: str, encoding: str) -> Mapping[str, Any]:
    try:
        import yaml
    except ImportError:
        raise _problem(
            "source_error", label, "YamlSource requires PyYAML; install it with pip install 'lauren[yaml]'"
        ) from None
    try:
        tree = yaml.safe_load(path.read_text(encoding=encoding))
    except yaml.YAMLError as exc:
        raise _problem("parse_error", label, str(exc)) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise _problem("source_error", label, str(exc)) from None
    if tree is None:
        return {}
    if not isinstance(tree, Mapping):
        raise _problem(
            "parse_error", label, f"expected a mapping at the top level, got {type(tree).__name__}"
        )
    return tree


def _no_lookup(_name: str) -> str | None:
    return None


def _secrets_dir_lookup(directory: Path, encoding: str) -> Callable[[str], str | None]:
    def lookup(name: str) -> str | None:
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return None
        for candidate in dict.fromkeys((name, name.lower())):
            target = directory / candidate
            if target.is_file():
                return target.read_text(encoding=encoding).rstrip("\r\n")
        return None

    return lookup


async def fill_async_layer(layer: _Layer, keys: Sequence[str]) -> None:
    """Await an async source and install its values on ``layer``."""
    try:
        loaded = await layer.source.load(list(keys))
    except Exception as exc:
        raise _problem("source_error", layer.label, f"{type(exc).__name__}: {exc}") from None
    if not isinstance(loaded, Mapping):
        raise _problem(
            "source_error", layer.label, f"load() returned {type(loaded).__name__}, expected a mapping"
        )
    values: dict[str, str] = {}
    for key, value in loaded.items():
        if not isinstance(value, str):
            raise _problem(
                "source_error",
                layer.label,
                f"load() returned a {type(value).__name__} for {key!r}, expected str",
            )
        values[str(key)] = value
    layer.lookup = values.get


# ---------------------------------------------------------------------------
# .env parser (stdlib only)
# ---------------------------------------------------------------------------


class DotenvSyntaxError(ValueError):
    """A ``.env`` file is malformed at ``line`` (1-based)."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = f"line {line}: {message}"


_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_INLINE_COMMENT = re.compile(r"\s#")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$"}


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``.env`` text; later duplicates win. Raises :class:`DotenvSyntaxError`."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: dict[str, str] = {}
    index = 0
    while index < len(lines):
        lineno = index + 1
        stripped = lines[index].strip()
        index += 1
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("export ", "export\t")):
            stripped = stripped[7:].lstrip()
        key, sep, rest = stripped.partition("=")
        key = key.strip()
        if not sep:
            raise DotenvSyntaxError(lineno, f"expected KEY=VALUE, got {stripped[:40]!r}")
        if not _KEY.fullmatch(key):
            raise DotenvSyntaxError(lineno, f"invalid variable name {key!r}")
        value_text = rest.lstrip()
        if value_text[:1] not in ("'", '"'):
            comment = _INLINE_COMMENT.search(rest)
            result[key] = (rest[: comment.start()] if comment else rest).strip()
            continue
        quote, body = value_text[0], value_text[1:]
        chunks: list[str] = []
        while True:
            end = _closing_quote(body, quote)
            if end >= 0:
                chunks.append(body[:end])
                tail = body[end + 1 :].strip()
                if tail and not tail.startswith("#"):
                    raise DotenvSyntaxError(index, f"unexpected text after the closing quote: {tail[:40]!r}")
                break
            chunks.append(body)
            if index >= len(lines):
                raise DotenvSyntaxError(lineno, f"unterminated {quote}-quoted value")
            body = lines[index]
            index += 1
        raw = "\n".join(chunks)
        result[key] = _unescape(raw) if quote == '"' else raw
    return result


def _closing_quote(body: str, quote: str) -> int:
    position = 0
    while position < len(body):
        char = body[position]
        if char == "\\" and quote == '"':
            position += 2
            continue
        if char == quote:
            return position
        position += 1
    return -1


def _unescape(raw: str) -> str:
    out: list[str] = []
    position = 0
    while position < len(raw):
        char = raw[position]
        if char == "\\" and position + 1 < len(raw):
            nxt = raw[position + 1]
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
            position += 2
            continue
        out.append(char)
        position += 1
    return "".join(out)


__all__ = [
    "AsyncSource",
    "DotenvSource",
    "DotenvSyntaxError",
    "EnvSource",
    "SecretsDirSource",
    "SourceProblem",
    "TomlSource",
    "YamlSource",
    "fill_async_layer",
    "is_async_source",
    "load_layer",
    "parse_dotenv",
    "source_label",
]
