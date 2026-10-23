"""Decoration-time behaviour of ``@interval``, ``@cron``, ``@run_once`` and ``@hosted_service``."""

from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from lauren import Scope, injectable
from lauren._di import INJECTABLE_META
from lauren.exceptions import DecoratorUsageError, LifecycleConfigError
from lauren.scheduling import (
    CronExpression,
    HostedServiceMeta,
    ScheduleMeta,
    ScheduleModule,
    cron,
    hosted_service,
    interval,
    run_once,
)


def _schedules(fn: object) -> list[ScheduleMeta]:
    return list(getattr(fn, "__lauren_schedule__"))


# ---------------------------------------------------------------------------
# Scheduled methods
# ---------------------------------------------------------------------------


def test_decorators_attach_metadata_and_return_the_same_function() -> None:
    def job() -> None: ...

    decorated = interval(seconds=60)(job)

    assert decorated is job
    (meta,) = _schedules(job)
    assert meta.kind == "interval"
    assert meta.seconds == 60.0
    assert meta.overlap == "skip" and meta.max_instances == 1


def test_stacking_accumulates_schedules() -> None:
    @cron("0 3 * * *")
    @interval(seconds=30)
    def job() -> None: ...

    assert [m.kind for m in _schedules(job)] == ["interval", "cron"]


@pytest.mark.parametrize("decorator", [interval, cron, run_once])
def test_bare_usage_is_rejected(decorator) -> None:
    with pytest.raises(DecoratorUsageError) as info:

        @decorator
        def job() -> None: ...

    assert "parentheses" in info.value.message


def test_interval_requires_seconds() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        interval()

    assert info.value.detail["reason"] == "missing_seconds"


def test_cron_requires_an_expression() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        cron()

    assert info.value.detail["reason"] == "missing_expression"


@pytest.mark.parametrize(
    ("kwargs", "option", "reason"),
    [
        ({"seconds": 0}, "seconds", "out_of_range"),
        ({"seconds": -5}, "seconds", "out_of_range"),
        ({"seconds": "60"}, "seconds", "invalid_number"),
        ({"seconds": True}, "seconds", "invalid_number"),
        ({"seconds": float("inf")}, "seconds", "invalid_number"),
        ({"seconds": 1, "initial_delay": -1}, "initial_delay", "out_of_range"),
        ({"seconds": 1, "mode": "sometimes"}, "mode", "invalid_choice"),
        ({"seconds": 1, "overlap": "maybe"}, "overlap", "invalid_choice"),
        ({"seconds": 1, "misfire": "retry"}, "misfire", "invalid_choice"),
        ({"seconds": 1, "max_instances": 0}, "max_instances", "out_of_range"),
        ({"seconds": 1, "timeout": 0}, "timeout", "out_of_range"),
        ({"seconds": 1, "jitter": -1}, "jitter", "out_of_range"),
        ({"seconds": 1, "misfire_grace": 0}, "misfire_grace", "out_of_range"),
        ({"seconds": 1, "exclusive": "yes"}, "exclusive", "invalid_flag"),
        ({"seconds": 1, "enabled": 1}, "enabled", "invalid_flag"),
        ({"seconds": 1, "name": ""}, "name", "invalid_name"),
    ],
)
def test_interval_validates_its_options(kwargs: dict, option: str, reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        interval(**kwargs)

    assert info.value.detail["option"] == option
    assert info.value.detail["reason"] == reason
    assert info.value.message.startswith("@interval:")


def test_timedelta_values_are_accepted() -> None:
    @interval(seconds=timedelta(minutes=2), initial_delay=timedelta(seconds=5), timeout=timedelta(seconds=30))
    def job() -> None: ...

    (meta,) = _schedules(job)
    assert (meta.seconds, meta.initial_delay, meta.timeout) == (120.0, 5.0, 30.0)


def test_allow_overlap_defaults_to_three_instances() -> None:
    @interval(seconds=1, overlap="allow")
    def job() -> None: ...

    assert _schedules(job)[0].max_instances == 3


def test_invalid_cron_fails_at_decoration_time() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        cron("61 * * * *")

    assert info.value.detail["field"] == "minute"


def test_unknown_cron_time_zone_fails_at_decoration_time() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        cron("0 3 * * *", tz="Nowhere/Land")

    assert info.value.detail["reason"] == "unknown_timezone"


def test_cron_accepts_a_parsed_expression_and_keeps_its_zone() -> None:
    @cron(CronExpression("0 3 * * *", tz="UTC"))
    def job() -> None: ...

    (meta,) = _schedules(job)
    assert meta.tz is timezone.utc
    assert meta.cron is not None and meta.cron.tz is None


def test_run_once_validates_delay() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        run_once(delay=-1)

    assert info.value.detail["option"] == "delay"


def test_schedule_on_a_class_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError) as info:

        @interval(seconds=1)
        class NotAMethod: ...

    assert info.value.detail["reason"] == "not_a_method"


def test_generator_job_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError) as info:

        @interval(seconds=1)
        async def job():
            yield 1

    assert info.value.detail["reason"] == "generator_job"


def test_schedule_on_a_staticmethod_descriptor_is_stored_on_the_descriptor() -> None:
    marked = interval(seconds=1)(staticmethod(lambda: None))

    assert isinstance(marked, staticmethod)
    assert _schedules(marked)[0].kind == "interval"


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (ScheduleMeta(kind="interval", seconds=60), "every 60s"),
        (
            ScheduleMeta(kind="interval", seconds=5, mode="fixed_delay", initial_delay=2),
            "every 5s after each run, first after 2s",
        ),
        (ScheduleMeta(kind="cron", cron=CronExpression("0 3 * * *")), "cron 0 3 * * * (UTC)"),
        (ScheduleMeta(kind="once", delay=10), "once, 10s after startup"),
    ],
)
def test_describe_is_human_readable(meta: ScheduleMeta, expected: str) -> None:
    assert meta.describe(timezone.utc) == expected


# ---------------------------------------------------------------------------
# Hosted services
# ---------------------------------------------------------------------------


def test_hosted_service_marks_a_singleton_injectable() -> None:
    @hosted_service()
    class Relay:
        async def run(self, ctx) -> None: ...

    assert Relay.__dict__["__lauren_hosted_service__"] == HostedServiceMeta()
    assert Relay.__dict__[INJECTABLE_META].scope == Scope.SINGLETON


def test_hosted_service_keeps_its_own_injectable_metadata() -> None:
    @hosted_service()
    @injectable(scope=Scope.REQUEST)
    class Relay:
        async def run(self, ctx) -> None: ...

    assert Relay.__dict__[INJECTABLE_META].scope == Scope.REQUEST  # rejected at startup instead


def test_hosted_service_bare_usage_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError):

        @hosted_service
        class Relay:
            async def run(self, ctx) -> None: ...


def test_hosted_service_positional_options_are_rejected() -> None:
    with pytest.raises(DecoratorUsageError):
        hosted_service("relay")


@pytest.mark.parametrize(
    "body",
    [
        "pass",
        "run = 5",
        "@staticmethod\n    async def run(ctx): ...",
        "async def run(self): ...",
        "async def run(self, a, b): ...",
        "async def run(self, ctx, *, flag): ...",
        "async def run(self, ctx):\n        yield ctx",
    ],
)
def test_hosted_service_without_a_proper_run_method_is_rejected(body: str) -> None:
    namespace: dict = {}
    exec(f"class Relay:\n    {body}\n", namespace)  # noqa: S102 - builds the class under test

    with pytest.raises(LifecycleConfigError) as info:
        hosted_service()(namespace["Relay"])

    assert info.value.detail == {"class": "Relay", "reason": "invalid_run_method"}
    assert "async def run(self, ctx: ServiceContext)" in info.value.message


@pytest.mark.parametrize(
    "signature",
    ["def run(self, ctx): ...", "async def run(self, *args): ...", "async def run(self, ctx, extra=1): ..."],
)
def test_hosted_service_accepts_compatible_run_signatures(signature: str) -> None:
    namespace: dict = {}
    exec(f"class Relay:\n    {signature}\n", namespace)  # noqa: S102

    assert hosted_service()(namespace["Relay"]) is namespace["Relay"]


def test_hosted_service_inherits_run_from_a_plain_base() -> None:
    class Base:
        async def run(self, ctx) -> None: ...

    @hosted_service()
    class Relay(Base): ...

    assert "__lauren_hosted_service__" in Relay.__dict__


def test_hosted_service_on_a_function_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError):
        hosted_service()(lambda: None)  # type: ignore[type-var]


@pytest.mark.parametrize(
    ("kwargs", "option"),
    [
        ({"restart": "sometimes"}, "restart"),
        ({"on_exhausted": "panic"}, "on_exhausted"),
        ({"max_restarts": -1}, "max_restarts"),
        ({"max_restarts": True}, "max_restarts"),
        ({"backoff": (1.0,)}, "backoff"),
        ({"backoff": (5.0, 1.0)}, "backoff"),
        ({"restart_window": 0}, "restart_window"),
        ({"start_timeout": 0}, "start_timeout"),
        ({"stop_timeout": -1}, "stop_timeout"),
        ({"wait_ready": "yes"}, "wait_ready"),
        ({"name": "  "}, "name"),
    ],
)
def test_hosted_service_validates_its_options(kwargs: dict, option: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        hosted_service(**kwargs)

    assert info.value.detail["option"] == option


# ---------------------------------------------------------------------------
# ScheduleModule.for_root
# ---------------------------------------------------------------------------


def test_for_root_returns_a_fresh_module_carrying_the_config() -> None:
    first = ScheduleModule.for_root()
    second = ScheduleModule.for_root(timezone="UTC", enabled=False)

    assert first is not second
    assert first.__dict__["__lauren_schedule_config__"].enabled is True
    assert second.__dict__["__lauren_schedule_config__"].enabled is False
    assert first.__name__ == "ScheduleModule[UTC]"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"timezone": "Atlantis/Main"}, "unknown_timezone"),
        ({"lock": object()}, "invalid_lock"),
        ({"clock": object()}, "invalid_clock"),
        ({"enabled": "no"}, "invalid_flag"),
        ({"job_drain_timeout": -1}, "invalid_number"),
        ({"job_drain_timeout": True}, "invalid_number"),
    ],
)
def test_for_root_validates_its_options(kwargs: dict, reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        ScheduleModule.for_root(**kwargs)

    assert info.value.detail["reason"] == reason
