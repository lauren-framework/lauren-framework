"""``JobContext``, ``ServiceContext`` and the bound logger."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

from lauren import DIContainer, OperationScope
from lauren._scheduling._context import bound_logger
from lauren.logging import InMemoryLogger, LogLevel, LogRecord
from lauren.scheduling import JobContext, ManualClock, ServiceContext

AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _service_context() -> ServiceContext:
    container = DIContainer()
    container.compile()
    return ServiceContext(
        name="svc",
        clock=ManualClock(),
        log=InMemoryLogger(),
        scope_factory=lambda name, values=None: OperationScope(container, name, values=values),
    )


def test_bound_logger_defaults_context_and_merges_fields() -> None:
    logger = InMemoryLogger()
    log = bound_logger(logger, "Job:purge", job="purge")

    log.debug("d")
    log.verbose("v")
    log.log("l")
    log.info("i", count=3)
    log.warn("w")
    log.warning("w2")
    log.error("e", context="Custom")
    log.log_record(LogRecord(level=LogLevel.INFO, message="raw"))

    assert [r.message for r in logger.records] == ["d", "v", "l", "i", "w", "w2", "e", "raw"]
    assert [r.context for r in logger.records][:6] == ["Job:purge"] * 6
    assert logger.records[6].context == "Custom"
    assert logger.records[3].extra == {"job": "purge", "count": 3}
    assert log.level == logger.level


def test_job_context_exposes_its_run() -> None:
    log = InMemoryLogger()
    ctx = JobContext(name="purge", scheduled_at=AT, fired_at=AT, log=log)

    assert (ctx.name, ctx.scheduled_at, ctx.fired_at, ctx.attempt) == ("purge", AT, AT, 1)
    assert ctx.log is log
    assert ctx.cancelled is False and ctx.scope is None
    assert repr(ctx) == "<JobContext 'purge' scheduled_at=2026-01-01T00:00:00+00:00>"


def test_mark_ready_before_start_sets_the_flag() -> None:
    ctx = _service_context()

    ctx.mark_ready()

    assert ctx.ready is True
    assert "ready" in repr(ctx)


async def test_mark_ready_from_a_thread_wakes_the_loop() -> None:
    ctx = _service_context()
    ctx._begin_attempt(0, asyncio.get_running_loop())

    thread = threading.Thread(target=ctx.mark_ready)
    thread.start()
    thread.join()
    await asyncio.wait_for(ctx._ready_event.wait(), timeout=5)

    assert ctx.ready


def test_mark_ready_after_the_loop_closed_is_harmless() -> None:
    ctx = _service_context()
    loop = asyncio.new_event_loop()
    ctx._begin_attempt(0, loop)
    loop.close()

    ctx.mark_ready()

    assert ctx.ready


def test_repr_reports_stopping() -> None:
    ctx = _service_context()

    ctx._request_stop()

    assert ctx.stop_requested
    assert "stopping" in repr(ctx)
