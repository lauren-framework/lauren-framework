"""Structured logging for lauren applications.

lauren ships two logger implementations that end users can choose between:

* :class:`ConsoleLogger` — NestJS-style, coloured, human-readable output.
  Ideal for local development and for ``tail -f``'ing in a terminal.
* :class:`JsonLogger` — one JSON object per line. Ideal for production where
  logs are piped into an aggregator (Loki, Datadog, CloudWatch, ...).

Both conform to the :class:`Logger` protocol, so users can also plug in
their own (e.g. a ``structlog``- or ``loguru``-backed implementation).
The framework emits lifecycle events through whatever logger the user
installs via ``LaurenFactory.create(..., logger=...)``.

Log methods mirror NestJS's: :meth:`log` (INFO), :meth:`warn`, :meth:`error`,
:meth:`debug`, :meth:`verbose`. Each accepts an optional ``context`` label
(``[RouterExplorer]``) and free-form ``extra`` keyword arguments for
structured logs.
"""

from __future__ import annotations

import enum
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, TextIO, runtime_checkable

from .decorators import injectable
from .types import Scope


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


class LogLevel(int, enum.Enum):
    """Log severity. Values chosen to match Python's ``logging`` levels."""

    DEBUG = 10
    VERBOSE = 15  # finer-grained than DEBUG (NestJS-style)
    INFO = 20
    WARN = 30
    ERROR = 40
    SILENT = 100

    @classmethod
    def parse(cls, value: "str | int | LogLevel") -> "LogLevel":
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            try:
                return cls(value)
            except ValueError:
                return cls.INFO
        if isinstance(value, str):
            v = value.strip().upper()
            aliases = {"WARNING": "WARN", "ERR": "ERROR"}
            v = aliases.get(v, v)
            if v in cls.__members__:
                return cls[v]
        return cls.INFO


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogRecord:
    """A single log event. Passed to :class:`Logger` implementations."""

    level: LogLevel
    message: str
    context: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    extra: dict[str, Any] = field(default_factory=dict)
    #: Monotonic OS process id; useful for multi-worker setups.
    pid: int = field(default_factory=os.getpid)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Logger(Protocol):
    """Structural interface for lauren loggers.

    Implementations only need to expose :meth:`log_record` (or the sugar
    wrappers below) and :attr:`level`.
    """

    level: LogLevel

    def log_record(self, record: LogRecord) -> None: ...

    def log(self, message: str, *, context: str = "", **extra: Any) -> None: ...
    def info(self, message: str, *, context: str = "", **extra: Any) -> None: ...
    def debug(self, message: str, *, context: str = "", **extra: Any) -> None: ...
    def verbose(self, message: str, *, context: str = "", **extra: Any) -> None: ...
    def warn(self, message: str, *, context: str = "", **extra: Any) -> None: ...
    def warning(self, message: str, *, context: str = "", **extra: Any) -> None: ...
    def error(self, message: str, *, context: str = "", **extra: Any) -> None: ...


class _BaseLogger:
    """Shared helpers for lauren's built-in loggers.

    Provides the NestJS-style sugar methods on top of a single
    :meth:`log_record` sink. Subclasses override :meth:`log_record`.
    """

    level: LogLevel = LogLevel.INFO

    def __init__(self, *, level: "str | int | LogLevel" = LogLevel.INFO) -> None:
        self.level = LogLevel.parse(level)

    # -- core --------------------------------------------------------------

    def log_record(self, record: LogRecord) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- sugar -------------------------------------------------------------

    def _emit(
        self,
        level: LogLevel,
        message: str,
        context: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        if int(level) < int(self.level):
            return
        self.log_record(
            LogRecord(
                level=level,
                message=message,
                context=context,
                extra=dict(extra or {}),
            )
        )

    def debug(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit(LogLevel.DEBUG, message, context, extra)

    def verbose(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit(LogLevel.VERBOSE, message, context, extra)

    def log(self, message: str, *, context: str = "", **extra: Any) -> None:
        """NestJS-compatible alias for INFO."""
        self._emit(LogLevel.INFO, message, context, extra)

    info = log  # Python-idiomatic alias

    def warn(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit(LogLevel.WARN, message, context, extra)

    warning = warn

    def error(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit(LogLevel.ERROR, message, context, extra)


# ---------------------------------------------------------------------------
# ConsoleLogger — pretty coloured output for dev
# ---------------------------------------------------------------------------


# ANSI colours (subset; broadly supported).
_ANSI = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "gray": "\x1b[90m",
}


_LEVEL_COLOURS = {
    LogLevel.DEBUG: _ANSI["gray"],
    LogLevel.VERBOSE: _ANSI["cyan"],
    LogLevel.INFO: _ANSI["green"],
    LogLevel.WARN: _ANSI["yellow"],
    LogLevel.ERROR: _ANSI["red"],
}


_LEVEL_LABELS = {
    LogLevel.DEBUG: "DEBUG",
    LogLevel.VERBOSE: "VERB ",
    LogLevel.INFO: "INFO ",
    LogLevel.WARN: "WARN ",
    LogLevel.ERROR: "ERROR",
}


@injectable(scope=Scope.SINGLETON, provides=(Logger,))
class ConsoleLogger(_BaseLogger):
    """Human-readable logger with optional ANSI colour.

    Output format::

        [Lauren] 18:22:01.123  INFO  [RouterExplorer] Mapped {GET /users}

    Colours auto-enable only when the destination stream is a TTY; override
    with ``use_colour=True/False`` to force a choice.
    """

    def __init__(
        self,
        *,
        level: "str | int | LogLevel" = LogLevel.INFO,
        stream: TextIO | None = None,
        use_colour: bool | None = None,
        name: str = "Lauren",
        show_pid: bool = False,
    ) -> None:
        super().__init__(level=level)
        self._stream: TextIO = stream or sys.stdout
        if use_colour is None:
            use_colour = bool(
                getattr(self._stream, "isatty", lambda: False)()
                and os.environ.get("NO_COLOR") is None
                and os.environ.get("TERM") != "dumb"
            )
        self._use_colour = use_colour
        self._name = name
        self._show_pid = show_pid
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def _colour(self, code: str, text: str) -> str:
        if not self._use_colour:
            return text
        return f"{code}{text}{_ANSI['reset']}"

    def _format(self, record: LogRecord) -> str:
        ts = record.timestamp.astimezone().strftime("%H:%M:%S.%f")[:-3]
        level_colour = _LEVEL_COLOURS.get(record.level, "")
        level_label = _LEVEL_LABELS.get(record.level, str(record.level.name))
        name = self._colour(_ANSI["magenta"] + _ANSI["bold"], f"[{self._name}]")
        pid = self._colour(_ANSI["dim"], f"{record.pid} ") if self._show_pid else ""
        time_part = self._colour(_ANSI["gray"], ts)
        level_part = self._colour(level_colour + _ANSI["bold"], level_label)
        context = (
            self._colour(_ANSI["yellow"], f"[{record.context}] ")
            if record.context
            else ""
        )
        message = record.message
        if record.extra:
            # Render extras as key=value suffix, compact and aligned right.
            rendered = " ".join(
                f"{k}={_compact_repr(v)}" for k, v in record.extra.items()
            )
            message = f"{message}  {self._colour(_ANSI['dim'], rendered)}"
        return f"{name} {pid}{time_part} {level_part} {context}{message}"

    def log_record(self, record: LogRecord) -> None:
        line = self._format(record) + "\n"
        with self._lock:
            self._stream.write(line)
            self._stream.flush()


# ---------------------------------------------------------------------------
# JsonLogger — one structured line per record
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON, provides=(Logger,))
class JsonLogger(_BaseLogger):
    """One JSON object per line, for production log aggregators.

    Schema::

        {
          "ts": "2026-04-24T18:22:01.123456+00:00",
          "level": "info",
          "logger": "Lauren",
          "context": "RouterExplorer",
          "message": "Mapped route",
          "pid": 12345,
          "method": "GET",
          "path": "/users"
        }

    ``extra`` keys are merged into the top-level object so log processors
    can index them directly.
    """

    def __init__(
        self,
        *,
        level: "str | int | LogLevel" = LogLevel.INFO,
        stream: TextIO | None = None,
        name: str = "Lauren",
    ) -> None:
        super().__init__(level=level)
        self._stream: TextIO = stream or sys.stdout
        self._name = name
        self._lock = threading.Lock()

    def log_record(self, record: LogRecord) -> None:
        payload: dict[str, Any] = {
            "ts": record.timestamp.isoformat(),
            "level": record.level.name.lower(),
            "logger": self._name,
            "message": record.message,
            "pid": record.pid,
        }
        if record.context:
            payload["context"] = record.context
        # Merge extras without clobbering the reserved keys.
        reserved = set(payload.keys())
        for k, v in record.extra.items():
            if k in reserved:
                payload[f"extra_{k}"] = v
            else:
                payload[k] = v
        line = json.dumps(payload, default=str, separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


# ---------------------------------------------------------------------------
# NullLogger — silent (useful for tests)
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON, provides=(Logger,))
class NullLogger(_BaseLogger):
    """Discards everything. Use in tests or when lauren must stay quiet."""

    def __init__(self, *, level: "str | int | LogLevel" = LogLevel.SILENT) -> None:
        super().__init__(level=level)

    def log_record(self, record: LogRecord) -> None:  # pragma: no cover
        return


# ---------------------------------------------------------------------------
# InMemoryLogger — test helper; captures records for assertions
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON, provides=(Logger,))
class InMemoryLogger(_BaseLogger):
    """Collects records in a list for unit tests."""

    def __init__(self, *, level: "str | int | LogLevel" = LogLevel.DEBUG) -> None:
        super().__init__(level=level)
        self.records: list[LogRecord] = []

    def log_record(self, record: LogRecord) -> None:
        self.records.append(record)

    def messages(self, *, level: LogLevel | None = None) -> list[str]:
        return [r.message for r in self.records if level is None or r.level == level]

    def contexts(self) -> list[str]:
        return [r.context for r in self.records if r.context]

    def clear(self) -> None:
        self.records.clear()


# ---------------------------------------------------------------------------
# Factory — build a logger from an env-style config
# ---------------------------------------------------------------------------


def default_logger(
    *,
    level: "str | int | LogLevel" = LogLevel.INFO,
    format: str = "auto",
    stream: TextIO | None = None,
    name: str = "Lauren",
) -> Logger:
    """Return a logger pre-configured from ``format``.

    * ``format="console"`` → coloured :class:`ConsoleLogger`.
    * ``format="json"`` → structured :class:`JsonLogger`.
    * ``format="silent"`` → :class:`NullLogger`.
    * ``format="auto"`` (default) → console if stdout is a TTY or the
      ``LAUREN_LOG_FORMAT`` env var is unset; otherwise JSON. Also honours
      the ``LAUREN_LOG_LEVEL`` env var.
    """
    env_level = os.environ.get("LAUREN_LOG_LEVEL")
    if env_level:
        level = env_level
    env_format = os.environ.get("LAUREN_LOG_FORMAT")
    fmt = (env_format or format).lower()
    if fmt == "silent":
        return NullLogger(level=level)
    if fmt == "json":
        return JsonLogger(level=level, stream=stream, name=name)
    if fmt == "console":
        return ConsoleLogger(level=level, stream=stream, name=name)
    # auto
    is_tty = bool(getattr(stream or sys.stdout, "isatty", lambda: False)())
    if is_tty:
        return ConsoleLogger(level=level, stream=stream, name=name)
    return JsonLogger(level=level, stream=stream, name=name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compact_repr(value: Any) -> str:
    """Produce a short, single-line representation for log extras."""
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    if isinstance(value, str):
        if any(ch.isspace() for ch in value) or len(value) > 40:
            return json.dumps(value)
        return value
    try:
        return json.dumps(value, default=str, separators=(",", ":"))
    except Exception:
        return repr(value)


def format_duration_ms(seconds: float) -> str:
    """Render a sub-second duration as ``12ms`` or ``1.23s``."""
    if seconds < 1.0:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds:.2f}s"


__all__ = [
    "Logger",
    "LogLevel",
    "LogRecord",
    "ConsoleLogger",
    "JsonLogger",
    "NullLogger",
    "InMemoryLogger",
    "default_logger",
    "format_duration_ms",
]
