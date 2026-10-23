"""``lauren.health`` building blocks: values, aggregation, ``@health_check``, options, indicators."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from lauren import GuardConfigError
from lauren._health._indicators import LoopLagMonitor, disk_probe, memory_probe, memory_supported, read_rss
from lauren._health._meta import HEALTH_CHECK_META, HealthCheckMeta
from lauren._health._module import HEALTH_OPTIONS_ATTR
from lauren._health import _runtime
from lauren._health._runtime import HealthRuntime, _CheckState, _normalise, _StillRunning, _ThreadRun
from lauren._health._types import aggregate
from lauren.exceptions import DecoratorUsageError
from lauren.health import HealthModule, HealthResult, HealthStatus, health_check

UP, DEGRADED, DOWN = HealthStatus.UP, HealthStatus.DEGRADED, HealthStatus.DOWN


# ---------------------------------------------------------------------------
# HealthResult
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "status"),
    [(HealthResult.up, UP), (HealthResult.degraded, DEGRADED), (HealthResult.down, DOWN)],
)
def test_result_constructors_set_status_and_details(build: Any, status: HealthStatus) -> None:
    result = build(latency_ms=1.5, reason="x")

    assert result.status is status
    assert result.details == {"latency_ms": 1.5, "reason": "x"}


def test_a_result_accepts_a_status_value() -> None:
    assert HealthResult("degraded").status is DEGRADED  # type: ignore[arg-type]


def test_a_result_rejects_an_unknown_status() -> None:
    with pytest.raises(ValueError):
        HealthResult("sideways")  # type: ignore[arg-type]


def test_a_result_rejects_non_mapping_details() -> None:
    with pytest.raises(TypeError, match="mapping"):
        HealthResult(UP, ["x"])  # type: ignore[arg-type]


def test_a_result_copies_its_details() -> None:
    details = {"a": 1}
    result = HealthResult(UP, details)

    details["a"] = 2

    assert result.details == {"a": 1}


def test_a_result_is_frozen() -> None:
    with pytest.raises(AttributeError):
        HealthResult.up().status = DOWN  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The return-value contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "status"), [(None, UP), (True, UP), (False, DOWN)])
def test_plain_return_values_map_to_a_status(raw: Any, status: HealthStatus) -> None:
    assert _normalise(raw).status is status


def test_a_returned_result_is_used_as_is() -> None:
    result = HealthResult.degraded(reason="slow")

    assert _normalise(result) is result


@pytest.mark.parametrize("raw", [1, "ok", {"status": "up"}])
def test_any_other_return_value_is_a_type_error(raw: Any) -> None:
    with pytest.raises(TypeError, match="return None, a bool or a HealthResult"):
        _normalise(raw)


# ---------------------------------------------------------------------------
# Aggregation truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pairs", "expected"),
    [
        ([], UP),
        ([(True, UP)], UP),
        ([(False, UP)], UP),
        ([(True, DEGRADED)], DEGRADED),
        ([(False, DEGRADED)], DEGRADED),
        ([(True, DOWN)], DOWN),
        ([(False, DOWN)], DEGRADED),
        ([(True, UP), (False, DOWN)], DEGRADED),
        ([(True, UP), (True, DEGRADED), (False, UP)], DEGRADED),
        ([(False, DOWN), (True, DOWN)], DOWN),
        ([(True, DEGRADED), (True, DOWN), (False, UP)], DOWN),
    ],
)
def test_aggregation(pairs: list[tuple[bool, HealthStatus]], expected: HealthStatus) -> None:
    assert aggregate(pairs) is expected


# ---------------------------------------------------------------------------
# @health_check
# ---------------------------------------------------------------------------


def test_the_decorator_records_metadata_and_returns_the_function() -> None:
    def ping(self: Any) -> bool:
        return True

    decorated = health_check("db", critical=False, timeout=1, cache_ttl=5, tags=("startup", "ready"))(ping)

    assert decorated is ping
    assert getattr(ping, HEALTH_CHECK_META) == [
        HealthCheckMeta(name="db", critical=False, timeout=1.0, cache_ttl=5.0, tags=("ready", "startup"))
    ]


def test_the_defaults_feed_readiness_with_a_two_second_timeout() -> None:
    @health_check("db")
    def ping(self: Any) -> None: ...

    assert getattr(ping, HEALTH_CHECK_META) == [
        HealthCheckMeta(name="db", critical=True, timeout=2.0, cache_ttl=0.0, tags=("ready",))
    ]


def test_stacking_registers_several_checks() -> None:
    @health_check("db-live", tags=("live",))
    @health_check("db")
    def ping(self: Any) -> None: ...

    assert [m.name for m in getattr(ping, HEALTH_CHECK_META)] == ["db", "db-live"]


@pytest.mark.parametrize("wrapper", [staticmethod, classmethod])
def test_the_marker_lands_on_the_function_above_a_descriptor(wrapper: Any) -> None:
    def ping(*args: Any) -> None: ...

    descriptor = health_check("db")(wrapper(ping))

    assert isinstance(descriptor, wrapper)
    assert getattr(ping, HEALTH_CHECK_META)[0].name == "db"


def test_bare_usage_is_rejected() -> None:
    def ping(self: Any) -> None: ...

    with pytest.raises(DecoratorUsageError, match='@health_check\\("database"\\)') as info:
        health_check(ping)  # type: ignore[arg-type]

    assert info.value.detail["decorator"] == "health_check"


@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"name": 3}, "name"),
        ({"name": "  "}, "name"),
        ({"name": "db", "critical": 1}, "critical"),
        ({"name": "db", "timeout": 0}, "timeout"),
        ({"name": "db", "timeout": -1.0}, "timeout"),
        ({"name": "db", "timeout": True}, "timeout"),
        ({"name": "db", "timeout": float("inf")}, "timeout"),
        ({"name": "db", "cache_ttl": -1}, "cache_ttl"),
        ({"name": "db", "cache_ttl": "5"}, "cache_ttl"),
        ({"name": "db", "tags": "ready"}, "tags"),
        ({"name": "db", "tags": None}, "tags"),
        ({"name": "db", "tags": ("readiness",)}, "tags"),
    ],
)
def test_invalid_arguments_are_rejected_at_decoration(kwargs: dict[str, Any], key: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        health_check(**kwargs)

    assert key in info.value.detail


def test_decorating_a_class_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError, match="decorates a method"):
        health_check("db")(type("NotAMethod", (), {}))


def test_an_empty_tag_tuple_is_allowed() -> None:
    @health_check("report-only", tags=())
    def ping(self: Any) -> None: ...

    assert getattr(ping, HEALTH_CHECK_META)[0].tags == ()


# ---------------------------------------------------------------------------
# HealthModule.for_root options
# ---------------------------------------------------------------------------


def test_for_root_returns_a_module_carrying_its_options() -> None:
    generated = HealthModule.for_root(path="/healthz/", version="1.2.3", disk_paths={Path("/"): 0})

    options = generated.__dict__[HEALTH_OPTIONS_ATTR]
    assert (options.path, options.version, options.details, options.format) == (
        "/healthz",
        "1.2.3",
        "never",
        "lauren",
    )
    assert options.disk_paths == ((str(Path("/")), 0),)


def test_each_for_root_call_generates_a_distinct_module() -> None:
    assert HealthModule.for_root() is not HealthModule.for_root()


@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"path": "health"}, "path"),
        ({"path": 1}, "path"),
        ({"details": "sometimes"}, "details"),
        ({"format": "xml"}, "format"),
        ({"event_loop_lag_ms": 0}, "event_loop_lag_ms"),
        ({"event_loop_lag_ms": "500"}, "event_loop_lag_ms"),
        ({"disk_paths": ["/"]}, "disk_paths"),
        ({"disk_paths": {"/": -1}}, "disk_paths"),
        ({"disk_paths": {"/": 1.5}}, "disk_paths"),
        ({"disk_paths": {1: 10}}, "disk_paths"),
        ({"max_rss_bytes": 0}, "max_rss_bytes"),
        ({"max_rss_bytes": True}, "max_rss_bytes"),
        ({"version": 1}, "version"),
        ({"report_status_code": "yes"}, "report_status_code"),
        ({"degraded_status": 100}, "degraded_status"),
        ({"degraded_status": 600}, "degraded_status"),
        ({"degraded_status": "200"}, "degraded_status"),
    ],
)
def test_invalid_options_are_rejected(kwargs: dict[str, Any], key: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        HealthModule.for_root(**kwargs)

    assert info.value.detail["helper"] == "HealthModule.for_root"
    assert key in info.value.detail


class _Guard:
    async def can_activate(self, ctx: Any) -> bool:
        return True


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"details": "when_authorized"}, "missing_details_guard"),
        ({"details": "when_authorized", "details_guard": object}, "invalid_details_guard"),
        ({"details": "when_authorized", "details_guard": _Guard()}, "invalid_details_guard"),
        ({"details": "always", "details_guard": _Guard}, "unused_details_guard"),
    ],
)
def test_details_guard_misconfiguration_is_a_guard_config_error(kwargs: dict[str, Any], reason: str) -> None:
    with pytest.raises(GuardConfigError) as info:
        HealthModule.for_root(**kwargs)

    assert info.value.detail["reason"] == reason


@pytest.mark.skipif(memory_supported(), reason="the memory indicator is supported here")
def test_the_memory_indicator_is_rejected_where_unsupported() -> None:  # pragma: no cover - Windows
    with pytest.raises(DecoratorUsageError) as info:
        HealthModule.for_root(max_rss_bytes=1 << 30)

    assert info.value.detail["reason"] == "unsupported_platform"


# ---------------------------------------------------------------------------
# Built-in indicators
# ---------------------------------------------------------------------------


def test_lag_without_samples_is_up() -> None:
    monitor = LoopLagMonitor(100)

    result = monitor.result()

    assert (result.status, result.details) == (UP, {"lag_ms": None, "threshold_ms": 100.0})


@pytest.mark.parametrize(
    ("lag", "status"), [(10.0, UP), (50.0, UP), (60.0, DEGRADED), (100.0, DEGRADED), (150.0, DOWN)]
)
def test_lag_status_follows_the_threshold(lag: float, status: HealthStatus) -> None:
    monitor = LoopLagMonitor(100)
    monitor.add_sample(lag)

    assert monitor.result().status is status


def test_lag_p99_is_taken_over_the_window() -> None:
    monitor = LoopLagMonitor(100, interval=0.5, window=2.0)
    for lag in (500.0, 1.0, 2.0, 3.0, 4.0):  # the first sample falls out of a four-sample window
        monitor.add_sample(lag)

    assert monitor.p99() == 4.0


async def test_the_sampler_records_lag_on_the_running_loop() -> None:
    monitor = LoopLagMonitor(100, interval=0.01)

    monitor.ensure_running()
    monitor.ensure_running()  # already running on this loop: no second task
    await asyncio.sleep(0.1)
    sampled = monitor.p99()
    monitor.stop()
    await asyncio.sleep(0)

    assert sampled is not None
    assert monitor.running is False
    assert monitor.p99() is None


def test_stopping_an_idle_sampler_is_a_no_op() -> None:
    monitor = LoopLagMonitor(100)

    monitor.stop()

    assert monitor.running is False


def test_stopping_a_sampler_from_another_loop_schedules_the_cancel() -> None:
    monitor = LoopLagMonitor(100, interval=0.01)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_start(monitor))
        task = monitor._task

        monitor.stop()  # no running loop here: cancelled through call_soon_threadsafe
        loop.run_until_complete(asyncio.sleep(0.01))

        assert task is not None and task.cancelled()
    finally:
        loop.close()


def test_stopping_a_sampler_whose_loop_is_closed_is_a_no_op() -> None:
    monitor = LoopLagMonitor(100, interval=0.01)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_start(monitor))
    task = monitor._task
    loop.close()

    monitor.stop()

    assert task is not None and not task.done()
    task._log_destroy_pending = False  # type: ignore[attr-defined]  # its loop is gone; nothing can run it


async def _start(monitor: LoopLagMonitor) -> None:
    monitor.ensure_running()
    await asyncio.sleep(0)


def test_disk_probe_is_up_with_enough_free_space(tmp_path: Path) -> None:
    result = disk_probe(str(tmp_path), 0)()

    assert result.status is UP
    assert result.details["path"] == str(tmp_path)
    assert result.details["free_bytes"] >= 0


def test_disk_probe_is_down_below_the_minimum(tmp_path: Path) -> None:
    result = disk_probe(str(tmp_path), 1 << 62)()

    assert result.status is DOWN
    assert result.details["min_free_bytes"] == 1 << 62


def test_disk_probe_raises_for_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        disk_probe(str(tmp_path / "missing"), 0)()


@pytest.mark.parametrize(("rss", "status"), [(100, UP), (1000, UP), (1001, DOWN)])
def test_memory_probe_compares_rss_with_the_limit(rss: int, status: HealthStatus) -> None:
    result = memory_probe(1000, reader=lambda: (rss, "current"))()

    assert result.status is status
    assert result.details == {"rss_bytes": rss, "max_rss_bytes": 1000, "measure": "current"}


@pytest.mark.skipif(sys.platform.startswith("win"), reason="no RSS reader on Windows")
def test_read_rss_reports_a_positive_size() -> None:
    rss, kind = read_rss()

    assert rss > 0
    assert kind == ("current" if sys.platform.startswith("linux") else "peak")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="no RSS reader on Windows")
def test_the_memory_indicator_is_supported_on_posix() -> None:
    assert memory_supported() is True


# ---------------------------------------------------------------------------
# Sync checks in worker threads
# ---------------------------------------------------------------------------


def _runtime_for_tests() -> HealthRuntime:
    return HealthRuntime(HealthModule.for_root().__dict__[HEALTH_OPTIONS_ATTR])


async def test_a_sync_run_abandoned_before_a_thread_starts_it_is_never_called(monkeypatch: Any) -> None:
    queued: list[Any] = []

    async def queued_then_cancelled(fn: Any, **kwargs: Any) -> Any:
        queued.append(fn)
        raise asyncio.CancelledError  # the timeout fired while the run waited for a thread

    monkeypatch.setattr(_runtime, "_run_sync", queued_then_cancelled)
    calls: list[int] = []
    state = _CheckState()

    with pytest.raises(asyncio.CancelledError):
        await _runtime_for_tests()._in_thread(state, lambda: calls.append(1), {})
    late = queued[0]()  # a worker thread picks the abandoned run up afterwards

    assert (late, calls) == (None, [])
    assert state.thread_run is not None and state.thread_run.busy is False


async def test_a_started_run_that_has_not_returned_blocks_the_next_one() -> None:
    state = _CheckState()
    hung = _ThreadRun()
    hung.begin()
    state.thread_run = hung

    with pytest.raises(_StillRunning):
        await _runtime_for_tests()._in_thread(state, lambda: True, {})


async def test_a_finished_run_lets_the_next_one_start() -> None:
    state = _CheckState()
    done = _ThreadRun()
    done.begin()
    done.finished = True
    state.thread_run = done

    assert await _runtime_for_tests()._in_thread(state, lambda: "ran", {}) == "ran"


def test_a_run_abandoned_before_it_began_cannot_begin() -> None:
    record = _ThreadRun()

    record.abandon()

    assert (record.begin(), record.busy) == (False, False)
