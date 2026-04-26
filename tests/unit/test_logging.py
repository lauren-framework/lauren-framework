"""Unit tests for :mod:`lauren.logging`."""

from __future__ import annotations

import io
import json


from lauren.logging import (
    ConsoleLogger,
    InMemoryLogger,
    JsonLogger,
    LogLevel,
    LogRecord,
    NullLogger,
    default_logger,
    format_duration_ms,
)


# ---------------------------------------------------------------------------
# LogLevel
# ---------------------------------------------------------------------------


class TestLogLevel:
    def test_numeric_ordering(self):
        assert LogLevel.DEBUG < LogLevel.INFO < LogLevel.WARN < LogLevel.ERROR

    def test_parse_string_case_insensitive(self):
        assert LogLevel.parse("info") is LogLevel.INFO
        assert LogLevel.parse("DEBUG") is LogLevel.DEBUG

    def test_parse_aliases(self):
        assert LogLevel.parse("warning") is LogLevel.WARN
        assert LogLevel.parse("err") is LogLevel.ERROR

    def test_parse_int(self):
        assert LogLevel.parse(20) is LogLevel.INFO

    def test_parse_unknown_defaults_to_info(self):
        assert LogLevel.parse("nonsense") is LogLevel.INFO


# ---------------------------------------------------------------------------
# ConsoleLogger
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TestConsoleLogger:
    def test_basic_output(self):
        buf = io.StringIO()
        logger = ConsoleLogger(level=LogLevel.DEBUG, stream=buf, use_colour=False)
        logger.log("hello", context="Router")
        line = buf.getvalue()
        assert "[Lauren]" in line
        assert "INFO" in line
        assert "[Router]" in line
        assert "hello" in line

    def test_colour_respected_when_tty(self):
        buf = io.StringIO()
        logger = ConsoleLogger(level=LogLevel.INFO, stream=buf, use_colour=True)
        logger.log("hi")
        assert "\x1b[" in buf.getvalue()

    def test_colour_suppressed_when_not_tty(self):
        buf = io.StringIO()
        logger = ConsoleLogger(level=LogLevel.INFO, stream=buf, use_colour=False)
        logger.log("hi")
        assert "\x1b[" not in buf.getvalue()

    def test_level_filters_below(self):
        buf = io.StringIO()
        logger = ConsoleLogger(level=LogLevel.WARN, stream=buf, use_colour=False)
        logger.debug("skipped")
        logger.info("also-skipped")
        logger.warn("shown")
        text = buf.getvalue()
        assert "skipped" not in text
        assert "also-skipped" not in text
        assert "shown" in text

    def test_all_levels_covered(self):
        buf = io.StringIO()
        logger = ConsoleLogger(level=LogLevel.DEBUG, stream=buf, use_colour=False)
        logger.debug("d")
        logger.verbose("v")
        logger.info("i")
        logger.warn("w")
        logger.error("e")
        for m in ("d", "v", "i", "w", "e"):
            assert m in buf.getvalue()

    def test_extras_rendered(self):
        buf = io.StringIO()
        logger = ConsoleLogger(level=LogLevel.INFO, stream=buf, use_colour=False)
        logger.log("mapped", context="R", method="GET", path="/u")
        text = buf.getvalue()
        assert "method=GET" in text
        assert "path=/u" in text

    def test_custom_name(self):
        buf = io.StringIO()
        logger = ConsoleLogger(stream=buf, use_colour=False, name="MyApp")
        logger.log("hi")
        assert "[MyApp]" in buf.getvalue()

    def test_thread_safe_flush(self):
        # Writes under the lock should each produce exactly one newline.
        buf = io.StringIO()
        logger = ConsoleLogger(stream=buf, use_colour=False)
        for i in range(50):
            logger.log(f"line {i}")
        assert buf.getvalue().count("\n") == 50


# ---------------------------------------------------------------------------
# JsonLogger
# ---------------------------------------------------------------------------


class TestJsonLogger:
    def test_one_object_per_line(self):
        buf = io.StringIO()
        logger = JsonLogger(level=LogLevel.INFO, stream=buf)
        logger.log("hello", context="Router", method="GET", status=200)
        line = buf.getvalue().strip()
        payload = json.loads(line)
        assert payload["level"] == "info"
        assert payload["context"] == "Router"
        assert payload["message"] == "hello"
        assert payload["method"] == "GET"
        assert payload["status"] == 200
        assert payload["logger"] == "Lauren"

    def test_timestamp_iso_format(self):
        buf = io.StringIO()
        JsonLogger(stream=buf).log("hi")
        payload = json.loads(buf.getvalue())
        # Should be a parseable ISO-8601 timestamp.
        from datetime import datetime

        datetime.fromisoformat(payload["ts"])

    def test_filters_below_level(self):
        buf = io.StringIO()
        JsonLogger(level=LogLevel.WARN, stream=buf).info("skip")
        assert buf.getvalue() == ""

    def test_reserved_keys_preserved(self):
        buf = io.StringIO()
        logger = JsonLogger(stream=buf)
        # Feed a record directly so we can attach extras that collide with
        # reserved keys — a real application would never do this at the
        # sugar-call site because Python's kwarg rules would forbid it, but
        # structured third-party dispatchers might pass LogRecord directly.
        logger.log_record(
            LogRecord(
                level=LogLevel.INFO,
                message="m",
                extra={
                    "level": "collision",
                    "message": "collision2",
                    "ok": True,
                },
            )
        )
        payload = json.loads(buf.getvalue())
        assert payload["level"] == "info"  # NOT overwritten by extra
        assert payload["message"] == "m"  # NOT overwritten
        assert payload["extra_level"] == "collision"
        assert payload["extra_message"] == "collision2"
        assert payload["ok"] is True

    def test_serializes_non_native_types(self):
        from datetime import datetime, timezone
        from uuid import UUID

        buf = io.StringIO()
        JsonLogger(stream=buf).log(
            "x",
            when=datetime(2024, 1, 1, tzinfo=timezone.utc),
            id=UUID("12345678-1234-5678-1234-567812345678"),
        )
        payload = json.loads(buf.getvalue())
        assert payload["when"].startswith("2024-01-01")
        assert payload["id"] == "12345678-1234-5678-1234-567812345678"


# ---------------------------------------------------------------------------
# NullLogger
# ---------------------------------------------------------------------------


class TestNullLogger:
    def test_discards_everything(self):
        logger = NullLogger()
        # None of these should raise or produce output.
        logger.log("a")
        logger.debug("b")
        logger.error("c")

    def test_level_is_silent(self):
        assert NullLogger().level is LogLevel.SILENT


# ---------------------------------------------------------------------------
# InMemoryLogger
# ---------------------------------------------------------------------------


class TestInMemoryLogger:
    def test_captures_records(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        logger.log("a", context="X")
        logger.error("b", context="Y", code=500)
        assert len(logger.records) == 2
        assert logger.records[0].context == "X"
        assert logger.records[1].extra["code"] == 500

    def test_filter_helpers(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        logger.info("i1")
        logger.error("e1")
        logger.info("i2")
        assert logger.messages(level=LogLevel.INFO) == ["i1", "i2"]
        assert logger.messages(level=LogLevel.ERROR) == ["e1"]

    def test_clear(self):
        logger = InMemoryLogger()
        logger.log("a")
        logger.clear()
        assert logger.records == []

    def test_respects_threshold(self):
        logger = InMemoryLogger(level=LogLevel.WARN)
        logger.info("x")
        logger.error("y")
        assert [r.message for r in logger.records] == ["y"]


# ---------------------------------------------------------------------------
# default_logger
# ---------------------------------------------------------------------------


class TestDefaultLogger:
    def test_explicit_json(self):
        logger = default_logger(format="json", stream=io.StringIO())
        assert isinstance(logger, JsonLogger)

    def test_explicit_console(self):
        logger = default_logger(format="console", stream=io.StringIO())
        assert isinstance(logger, ConsoleLogger)

    def test_silent(self):
        assert isinstance(default_logger(format="silent"), NullLogger)

    def test_env_override_level(self, monkeypatch):
        monkeypatch.setenv("LAUREN_LOG_LEVEL", "ERROR")
        logger = default_logger(format="json", stream=io.StringIO())
        assert logger.level is LogLevel.ERROR

    def test_env_override_format(self, monkeypatch):
        monkeypatch.setenv("LAUREN_LOG_FORMAT", "json")
        buf = io.StringIO()
        logger = default_logger(format="console", stream=buf)
        assert isinstance(logger, JsonLogger)


class TestFormatDurationMs:
    def test_sub_second(self):
        assert "ms" in format_duration_ms(0.0123)

    def test_multi_second(self):
        assert "s" in format_duration_ms(1.5)
