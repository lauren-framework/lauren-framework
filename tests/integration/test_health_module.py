"""``HealthModule.for_root`` end to end: probe semantics, execution model, details policy, startup checks."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import pytest

from lauren import (
    PUBLIC_METADATA_KEY,
    AppLifecycleState,
    DuplicateBindingError,
    ExecutionContext,
    HealthChanged,
    Inject,
    LaurenFactory,
    LifecycleConfigError,
    RouterConflictError,
    Scope,
    SignalBus,
    StartupError,
    Token,
    UnauthorizedError,
    UnresolvableParameterError,
    controller,
    get,
    injectable,
    module,
    pre_destruct,
    use_value,
)
from lauren._health._indicators import memory_supported
from lauren._health._runtime import HealthRuntime
from lauren.health import HealthModule, HealthResult, HealthStatus, health_check
from lauren.testing import TestClient


@controller("/")
class Home:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


def build(
    *providers: Any, signals: SignalBus | None = None, global_guards: Any = None, **options: Any
) -> Any:
    """An app with ``Home``, ``providers`` and a ``HealthModule.for_root(**options)``."""

    @module(imports=[HealthModule.for_root(**options)], providers=list(providers), controllers=[Home])
    class AppModule:
        pass

    return LaurenFactory.create(AppModule, signals=signals, global_guards=global_guards)


def runtime_of(app: Any) -> HealthRuntime:
    runtime: HealthRuntime = app.container._singletons[HealthRuntime]
    return runtime


async def call(app: Any, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
    """Drive one GET through the ASGI callable without starting the app (unlike ``TestClient``)."""
    status = 0
    out_headers: dict[str, str] = {}
    body = bytearray()
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, out_headers
        if message["type"] == "http.response.start":
            status = message["status"]
            out_headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return status, out_headers, json.loads(body) if body else None


@injectable(scope=Scope.SINGLETON)
class Database:
    """A dependency whose health a test controls."""

    def __init__(self) -> None:
        self.result: Any = True
        self.calls = 0

    @health_check("database")
    async def ping(self) -> Any:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@injectable(scope=Scope.SINGLETON)
class Search:
    @health_check("search", critical=False)
    async def ping(self) -> HealthResult:
        return HealthResult.down(reason="index unavailable")


def database(app: Any) -> Database:
    db: Database = app.container._singletons[Database]
    return db


# ---------------------------------------------------------------------------
# Probe semantics and the lifecycle state
# ---------------------------------------------------------------------------


async def test_probes_before_startup_fail_readiness_and_startup_but_not_liveness() -> None:
    app = build(Database)

    results = {path: await call(app, path) for path in ("/health/live", "/health/ready", "/health/startup")}

    assert {path: (r[0], r[2]) for path, r in results.items()} == {
        "/health/live": (200, {"status": "up"}),
        "/health/ready": (503, {"status": "down"}),
        "/health/startup": (503, {"status": "down"}),
    }


async def test_the_report_before_startup_runs_no_checks() -> None:
    app = build(Database)

    status, _, body = await call(app, "/health")

    assert (status, body) == (503, {"status": "down", "lifecycle": "created", "checks": {}})


def test_every_probe_passes_once_started() -> None:
    client = TestClient(build(Database))

    responses = [client.get(path) for path in ("/health/live", "/health/ready", "/health/startup")]

    assert [(r.status_code, r.json()) for r in responses] == [(200, {"status": "up"})] * 3


def test_probe_responses_are_json_and_never_cached() -> None:
    response = TestClient(build(Database)).get("/health/ready")

    assert response.header("content-type") == "application/json"
    assert response.header("cache-control") == "no-store"


def test_draining_fails_readiness_while_requests_are_still_served() -> None:
    app = build(Database)
    client = TestClient(app)

    app.begin_draining(reason="preStop")

    assert client.get("/health/ready").status_code == 503
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/startup").status_code == 200
    assert client.get("/").json() == {"ok": True}
    assert client.get("/health").json()["lifecycle"] == "draining"


async def test_every_probe_fails_after_shutdown() -> None:
    app = build(Database)
    await app.startup()
    await app.shutdown()

    statuses = [(await call(app, path))[0] for path in ("/health/live", "/health/ready", "/health/startup")]

    assert statuses == [503, 503, 503]


def test_a_failed_critical_check_fails_readiness_but_not_liveness() -> None:
    app = build(Database)
    client = TestClient(app)
    database(app).result = False

    assert (client.get("/health/ready").status_code, client.get("/health/live").status_code) == (503, 200)


def test_a_failed_non_critical_check_only_degrades_readiness() -> None:
    client = TestClient(build(Database, Search))

    ready = client.get("/health/ready")
    report = client.get("/health")

    assert (ready.status_code, ready.json()) == (200, {"status": "degraded"})
    assert (report.status_code, report.json()["status"]) == (200, "degraded")


def test_degraded_status_sets_the_code_of_degraded_answers() -> None:
    client = TestClient(build(Database, Search, degraded_status=429))

    assert client.get("/health/ready").status_code == 429


def test_a_live_tagged_check_feeds_liveness_only() -> None:
    @injectable(scope=Scope.SINGLETON)
    class Heart:
        @health_check("heartbeat", tags=("live",))
        def beat(self) -> bool:
            return False

    client = TestClient(build(Heart))

    assert (client.get("/health/live").status_code, client.get("/health/ready").status_code) == (503, 200)


def test_a_startup_tagged_check_feeds_the_startup_probe() -> None:
    @injectable(scope=Scope.SINGLETON)
    class Migrations:
        @health_check("migrations", tags=("startup",))
        def applied(self) -> bool:
            return False

    client = TestClient(build(Migrations))

    assert (client.get("/health/startup").status_code, client.get("/health/ready").status_code) == (503, 200)


def test_the_report_is_down_when_a_critical_check_is_down() -> None:
    app = build(Database)
    client = TestClient(app)
    database(app).result = HealthResult.down(reason="replica lag")

    report = client.get("/health")

    assert (report.status_code, report.json()) == (
        503,
        {"status": "down", "lifecycle": "ready", "checks": {"database": {"status": "down"}}},
    )


def test_report_status_code_false_answers_200_when_down() -> None:
    app = build(Database, report_status_code=False)
    client = TestClient(app)
    database(app).result = False

    report = client.get("/health")

    assert (report.status_code, report.json()["status"]) == (200, "down")


# ---------------------------------------------------------------------------
# Report content and the details policy
# ---------------------------------------------------------------------------


def test_the_report_shows_details_when_always() -> None:
    client = TestClient(build(Database, Search, details="always", version="1.4.2"))

    body = client.get("/health").json()

    assert body["version"] == "1.4.2"
    assert body["checks"]["database"]["status"] == "up"
    assert body["checks"]["database"]["critical"] is True
    assert body["checks"]["database"]["latency_ms"] >= 0
    assert body["checks"]["search"] == {
        "status": "down",
        "critical": False,
        "latency_ms": body["checks"]["search"]["latency_ms"],
        "reason": "index unavailable",
    }


def test_details_never_shows_only_names_and_statuses() -> None:
    app = build(Database, Search)
    client = TestClient(app)
    database(app).result = RuntimeError("postgres://admin:secret@db")

    response = client.get("/health")

    assert response.json()["checks"] == {"database": {"status": "down"}, "search": {"status": "down"}}
    assert "secret" not in response.text


def test_a_raising_check_reports_the_error_when_details_are_shown() -> None:
    app = build(Database, details="always")
    client = TestClient(app)
    database(app).result = ConnectionError("refused")

    entry = client.get("/health").json()["checks"]["database"]

    assert (entry["status"], entry["error"]) == ("down", "ConnectionError: refused")


def test_a_check_returning_garbage_is_down_with_a_type_error() -> None:
    app = build(Database, details="always")
    client = TestClient(app)
    database(app).result = "fine"

    entry = client.get("/health").json()["checks"]["database"]

    assert entry["status"] == "down"
    assert entry["error"].startswith("TypeError: a health check returned str")


def test_probes_never_show_details() -> None:
    app = build(Database, details="always")
    client = TestClient(app)
    database(app).result = RuntimeError("secret")

    assert client.get("/health/ready").json() == {"status": "down"}


@injectable()
class Policy:
    allowed = "letmein"


class AdminGuard:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return ctx.request.headers.get("x-admin") == self.policy.allowed


@pytest.mark.parametrize(("headers", "shown"), [({"x-admin": "letmein"}, True), ({}, False)])
def test_details_when_authorized_ask_the_guard(headers: dict[str, str], shown: bool) -> None:
    app = build(Database, Policy, details="when_authorized", details_guard=AdminGuard)
    client = TestClient(app)
    database(app).result = False

    response = client.get("/health", headers=headers)

    assert response.status_code == 503  # never 403: a denial only hides the details
    assert ("critical" in response.json()["checks"]["database"]) is shown


def test_a_details_guard_raising_an_http_error_hides_the_details() -> None:
    class StrictGuard:
        async def can_activate(self, ctx: ExecutionContext) -> bool:
            raise UnauthorizedError("token required")

    client = TestClient(build(Database, details="when_authorized", details_guard=StrictGuard))

    response = client.get("/health")

    assert (response.status_code, response.json()["checks"]) == (200, {"database": {"status": "up"}})


def test_the_ietf_format_answers_health_json() -> None:
    app = build(Database, Search, details="always", format="ietf")
    client = TestClient(app)
    database(app).result = ValueError("bad")

    report = client.get("/health")
    ready = client.get("/health/ready")

    assert report.header("content-type") == "application/health+json"
    body = report.json()
    assert body["status"] == "fail"
    assert body["checks"]["database"][0]["status"] == "fail"
    assert body["checks"]["database"][0]["output"] == "ValueError: bad"
    assert body["checks"]["search"][0]["status"] == "fail"
    assert ready.json() == {"status": "fail"}


def test_the_ietf_format_maps_degraded_to_warn() -> None:
    client = TestClient(build(Database, Search, format="ietf"))

    assert client.get("/health").json()["status"] == "warn"


# ---------------------------------------------------------------------------
# Routing: public metadata, OpenAPI
# ---------------------------------------------------------------------------


class RequireAuth:
    """A global guard that honours the well-known public key."""

    seen: list[Any] = []

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        RequireAuth.seen.append(ctx.get_metadata(PUBLIC_METADATA_KEY))
        return bool(ctx.get_metadata(PUBLIC_METADATA_KEY, False))


def test_probe_routes_carry_the_public_key_so_a_global_guard_can_skip_them() -> None:
    RequireAuth.seen = []
    client = TestClient(build(Database, global_guards=[RequireAuth]))

    probe = client.get("/health/ready")
    private = client.get("/")

    assert (probe.status_code, private.status_code) == (200, 403)
    assert RequireAuth.seen == [True, None]


def test_probe_routes_stay_out_of_openapi() -> None:
    app = build(Database)

    assert "/" in app.openapi()["paths"]
    assert not [path for path in app.openapi()["paths"] if path.startswith("/health")]


def test_the_path_option_moves_the_probes() -> None:
    client = TestClient(build(Database, path="/healthz"))

    assert client.get("/healthz/ready").status_code == 200
    assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# Execution model: timeouts, single-flight, cache
# ---------------------------------------------------------------------------


def test_a_slow_check_times_out_as_down() -> None:
    @injectable(scope=Scope.SINGLETON)
    class Hung:
        @health_check("hung", timeout=0.05)
        async def ping(self) -> None:
            await asyncio.sleep(30)

    client = TestClient(build(Hung, details="always"))

    started = time.monotonic()
    entry = client.get("/health").json()["checks"]["hung"]
    elapsed = time.monotonic() - started

    assert (entry["status"], entry["reason"]) == ("down", "timeout")
    assert elapsed < 10  # bounded by the check's timeout, not by the 30 s sleep


async def test_a_hung_sync_check_times_out_and_does_not_pile_up_threads() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    @injectable(scope=Scope.SINGLETON)
    class Blocking:
        @health_check("blocking", timeout=1.0)
        def ping(self) -> bool:
            calls.append(1)
            entered.set()
            release.wait(10)
            return True

    app = build(Blocking, details="always")
    await app.startup()
    try:
        first = (await call(app, "/health"))[2]["checks"]["blocking"]
        assert await asyncio.to_thread(entered.wait, 10)  # the hung run occupies its thread
        second = (await call(app, "/health"))[2]["checks"]["blocking"]
    finally:
        release.set()

    assert (first["status"], first["reason"]) == ("down", "timeout")
    assert (second["reason"], second["error"]) == ("timeout", "the previous run is still running")
    assert len(calls) == 1


async def test_an_inner_timeout_error_is_an_error_not_a_timeout() -> None:
    @injectable(scope=Scope.SINGLETON)
    class Client:
        @health_check("upstream")
        async def ping(self) -> None:
            raise TimeoutError("upstream took too long")

    app = build(Client, details="always")
    await app.startup()

    entry = (await call(app, "/health"))[2]["checks"]["upstream"]

    assert entry["error"] == "TimeoutError: upstream took too long"
    assert "reason" not in entry


async def test_concurrent_probes_share_one_run() -> None:
    calls: list[int] = []

    @injectable(scope=Scope.SINGLETON)
    class Slow:
        @health_check("slow")
        async def ping(self) -> bool:
            calls.append(1)
            await asyncio.sleep(0.05)
            return True

    app = build(Slow)
    await app.startup()

    responses = await asyncio.gather(*(call(app, "/health/ready") for _ in range(10)))

    assert [r[0] for r in responses] == [200] * 10
    assert len(calls) == 1


def test_a_cached_result_is_reused_until_its_ttl_expires() -> None:
    @injectable(scope=Scope.SINGLETON)
    class Cached:
        calls = 0

        @health_check("cached", cache_ttl=30)
        async def ping(self) -> bool:
            Cached.calls += 1
            return True

    app = build(Cached, details="always")
    client = TestClient(app)
    now = [1000.0]
    runtime_of(app)._clock = lambda: now[0]

    first = client.get("/health").json()["checks"]["cached"]
    second = client.get("/health").json()["checks"]["cached"]
    now[0] += 31
    third = client.get("/health").json()["checks"]["cached"]

    assert ("cached" in first, second.get("cached"), "cached" in third) == (False, True, False)
    assert Cached.calls == 2


def test_without_a_ttl_every_probe_runs_the_check() -> None:
    app = build(Database)
    client = TestClient(app)

    client.get("/health/ready")
    client.get("/health/ready")

    assert database(app).calls == 2


def test_a_check_resolves_its_dependencies_in_a_fresh_scope_per_run() -> None:
    events: list[str] = []

    @injectable(scope=Scope.REQUEST)
    class Connection:
        def __init__(self) -> None:
            events.append("open")

        @pre_destruct
        def close(self) -> None:
            events.append("close")

    @injectable(scope=Scope.SINGLETON)
    class Pool:
        @health_check("pool")
        async def ping(self, conn: Connection) -> bool:
            events.append("ping")
            return isinstance(conn, Connection)

    client = TestClient(build(Pool, Connection))

    assert client.get("/health/ready").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert events == ["open", "ping", "close"] * 2


def test_static_and_class_method_checks_run() -> None:
    @injectable(scope=Scope.SINGLETON)
    class Probes:
        @health_check("static")
        @staticmethod
        def static_check() -> bool:
            return True

        @classmethod
        @health_check("class")
        def class_check(cls) -> HealthResult:
            return HealthResult.degraded(owner=cls.__name__)

    body = TestClient(build(Probes, details="always")).get("/health").json()

    assert body["checks"]["static"]["status"] == "up"
    assert (body["checks"]["class"]["status"], body["checks"]["class"]["owner"]) == ("degraded", "Probes")


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def test_status_changes_emit_health_changed() -> None:
    signals = SignalBus()
    changes: list[tuple[str | None, HealthStatus | None, HealthStatus | None]] = []
    signals.on(HealthChanged)(lambda e: changes.append((e.name, e.previous, e.current)))
    app = build(Database, signals=signals)
    client = TestClient(app)

    client.get("/health")
    database(app).result = False
    client.get("/health")
    client.get("/health")

    up, down = HealthStatus.UP, HealthStatus.DOWN
    assert changes == [("database", None, up), (None, None, up), ("database", up, down), (None, up, down)]


def test_a_failing_check_signal_carries_the_error() -> None:
    signals = SignalBus()
    details: list[dict[str, Any]] = []
    signals.on(HealthChanged)(lambda e: details.append(e.details) if e.name else None)
    app = build(Database, signals=signals)
    client = TestClient(app)
    database(app).result = OSError("disk")

    client.get("/health/ready")

    assert details == [{"error": "OSError: disk"}]


# ---------------------------------------------------------------------------
# Built-in indicators
# ---------------------------------------------------------------------------


def test_the_disk_indicator_feeds_readiness(tmp_path: Path) -> None:
    client = TestClient(build(disk_paths={tmp_path: 1 << 62}))

    report = client.get("/health").json()

    assert report["checks"] == {f"disk:{tmp_path}": {"status": "down"}}
    assert client.get("/health/ready").status_code == 503


@pytest.mark.skipif(not memory_supported(), reason="no RSS reader on this platform")
@pytest.mark.parametrize(("limit", "status"), [(1, 503), (1 << 50, 200)])
def test_the_memory_indicator_feeds_readiness(limit: int, status: int) -> None:
    client = TestClient(build(max_rss_bytes=limit))

    assert client.get("/health/ready").status_code == status


async def test_the_event_loop_sampler_runs_while_ready() -> None:
    app = build(event_loop_lag_ms=500)
    monitor = runtime_of(app).monitor
    assert monitor is not None

    await app.startup()
    running_when_ready = monitor.running
    app.begin_draining()
    await asyncio.sleep(0)

    assert (running_when_ready, monitor.running) == (True, False)
    await app.shutdown()


async def test_event_loop_lag_fails_liveness() -> None:
    app = build(event_loop_lag_ms=100, details="always")
    await app.startup()
    monitor = runtime_of(app).monitor
    assert monitor is not None
    healthy = await call(app, "/health/live")

    monitor.add_sample(5000.0)
    wedged = await call(app, "/health/live")
    report = (await call(app, "/health"))[2]

    assert (healthy[0], wedged[0]) == (200, 503)
    assert report["checks"]["event_loop"]["lag_ms"] == 5000.0
    await app.shutdown()


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def test_a_check_on_a_request_scoped_provider_fails_startup() -> None:
    @injectable(scope=Scope.REQUEST)
    class PerRequest:
        @health_check("per-request")
        async def ping(self) -> bool:
            return True

    with pytest.raises(LifecycleConfigError) as info:
        build(PerRequest)

    assert info.value.detail == {
        "check": "per-request",
        "provider": "PerRequest",
        "method": "ping",
        "scope": "request",
        "reason": "non_singleton_health_check",
    }


def test_checks_are_validated_even_without_the_health_module() -> None:
    @injectable(scope=Scope.TRANSIENT)
    class Transient:
        @health_check("transient")
        def ping(self) -> bool:
            return True

    @module(providers=[Transient])
    class NoHealthModule:
        pass

    with pytest.raises(LifecycleConfigError):
        LaurenFactory.create(NoHealthModule)


def test_duplicate_check_names_fail_startup_naming_both_providers() -> None:
    @injectable(scope=Scope.SINGLETON)
    class Primary:
        @health_check("database")
        def ping(self) -> bool:
            return True

    with pytest.raises(DuplicateBindingError) as info:
        build(Database, Primary)

    assert info.value.detail["check"] == "database"
    assert info.value.detail["providers"] == ["Database.ping", "Primary.ping"]


@pytest.mark.parametrize("name", ["lifecycle", "event_loop", "memory", "disk:/"])
def test_built_in_indicator_names_are_reserved(name: str) -> None:
    @injectable(scope=Scope.SINGLETON)
    class Clash:
        @health_check(name)
        def ping(self) -> bool:
            return True

    with pytest.raises(DuplicateBindingError) as info:
        build(Clash)

    assert info.value.detail["reason"] == "reserved_health_check_name"


def test_a_check_with_an_unresolvable_parameter_fails_startup() -> None:
    @injectable(scope=Scope.SINGLETON)
    class Needy:
        @health_check("needy")
        async def ping(self, region: str) -> bool:
            return True

    with pytest.raises(UnresolvableParameterError) as info:
        build(Needy)

    assert (info.value.detail["target"], info.value.detail["param"]) == ("Needy.ping", "region")


def test_importing_the_health_module_twice_at_one_path_conflicts() -> None:
    @module(imports=[HealthModule.for_root(), HealthModule.for_root()])
    class Twice:
        pass

    with pytest.raises(RouterConflictError):
        LaurenFactory.create(Twice)


def test_importing_health_modules_with_different_options_fails_startup() -> None:
    @module(imports=[HealthModule.for_root(), HealthModule.for_root(path="/healthz")])
    class Conflicting:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(Conflicting)

    assert info.value.detail["reason"] == "conflicting_health_options"


# ---------------------------------------------------------------------------
# Multi-app isolation
# ---------------------------------------------------------------------------

HEALTHY = Token("healthy")


@injectable(scope=Scope.SINGLETON)
class Flagged:
    def __init__(self, healthy: Annotated[bool, Inject(HEALTHY)]) -> None:
        self.healthy = healthy

    @health_check("flagged")
    def ping(self) -> bool:
        return self.healthy


@module(imports=[HealthModule.for_root()], providers=[Flagged])
class SharedModule:
    pass


def test_two_apps_from_one_module_keep_separate_health() -> None:
    up = LaurenFactory.create(SharedModule, global_providers=[use_value(provide=HEALTHY, value=True)])
    down = LaurenFactory.create(SharedModule, global_providers=[use_value(provide=HEALTHY, value=False)])

    statuses = (
        TestClient(up).get("/health/ready").status_code,
        TestClient(down).get("/health/ready").status_code,
    )

    assert statuses == (200, 503)
    assert runtime_of(up) is not runtime_of(down)
    assert (up.lifecycle_state, down.lifecycle_state) == (AppLifecycleState.READY, AppLifecycleState.READY)
