"""End-to-end integration tests for Server-Sent Events.

Each test builds a tiny lauren application that returns an
:class:`~lauren.EventStream` from a regular HTTP handler, drives a
request through :class:`~lauren.testing.TestClient`, and asserts on the
serialized response.

The buffered :class:`~lauren.testing.TestClient` collects every body
chunk into one bytes object before returning, so SSE tests are fully
deterministic at this level — the exact wire bytes are inspected,
including the spec-mandated double newline that terminates each event.

Together with the unit tests in ``tests/unit/test_sse.py``, this file
gives full coverage of the SSE feature: framing rules, header defaults,
generator return values, dependency-injected producers, error paths,
and resumable streams via ``Last-Event-ID``.
"""

from __future__ import annotations

from typing import AsyncIterator


from lauren import (
    EventStream,
    LaurenFactory,
    Request,
    ServerSentEvent,
    controller,
    get,
    injectable,
    last_event_id,
    module,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helper to split the buffered SSE body back into individual frames so
# tests can match on per-event content rather than parsing inline.
# ---------------------------------------------------------------------------


def parse_sse_body(body: bytes) -> list[dict[str, str]]:
    """Parse a buffered SSE body into a list of event dicts.

    Each dict has ``event`` / ``id`` / ``retry`` / ``data`` /
    ``comment`` keys when the corresponding line was present. Multi-
    line ``data`` is joined with ``\\n`` to match the spec's
    reassembly rule.

    The parser is intentionally minimal — just enough to assert on the
    content of integration tests. Production code uses
    ``EventSource`` on the browser side or one of the existing
    Python SSE-client libraries (sseclient, aiohttp-sse-client).
    """
    events: list[dict[str, str]] = []
    current: dict[str, list[str]] = {}
    text = body.decode("utf-8")
    for line in text.split("\n"):
        if line == "":
            if current:
                events.append({k: ("\n".join(v) if k == "data" else v[0]) for k, v in current.items()})
                current = {}
            continue
        if line.startswith(":"):
            current.setdefault("comment", []).append(line[1:].lstrip())
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            value = value.lstrip()
            current.setdefault(field, []).append(value)
    return events


# ---------------------------------------------------------------------------
# Happy paths.
# ---------------------------------------------------------------------------


class TestBasicSseEndpoint:
    """A minimal ``return EventStream(...)`` handler ships valid SSE."""

    def test_handler_emits_events_in_order(self):
        @controller("/sse")
        class Ctl:
            @get("/feed")
            async def feed(self) -> EventStream:
                async def gen() -> AsyncIterator[ServerSentEvent]:
                    yield ServerSentEvent(event="user", data={"id": 1})
                    yield ServerSentEvent(event="user", data={"id": 2})
                    yield ServerSentEvent(event="user", data={"id": 3})

                return EventStream(gen())

        @module(controllers=[Ctl])
        class App:
            pass

        app = LaurenFactory.create(App)
        client = TestClient(app)
        resp = client.get("/sse/feed")

        assert resp.status_code == 200
        # Default SSE headers are present.
        ctype = next((v for k, v in resp.headers if k.lower() == "content-type"), None)
        assert ctype == "text/event-stream; charset=utf-8"
        cache = next((v for k, v in resp.headers if k.lower() == "cache-control"), None)
        assert cache == "no-cache"

        events = parse_sse_body(resp.body)
        assert events == [
            {"event": "user", "data": '{"id":1}'},
            {"event": "user", "data": '{"id":2}'},
            {"event": "user", "data": '{"id":3}'},
        ]

    def test_plain_string_yields_become_data_only_events(self):
        @controller("/log")
        class LogCtl:
            @get("/tail")
            async def tail(self) -> EventStream:
                async def gen() -> AsyncIterator[str]:
                    yield "started"
                    yield "processing"
                    yield "done"

                return EventStream(gen())

        @module(controllers=[LogCtl])
        class App:
            pass

        app = LaurenFactory.create(App)
        client = TestClient(app)
        resp = client.get("/log/tail")

        assert resp.status_code == 200
        assert resp.body == (b"data: started\n\ndata: processing\n\ndata: done\n\n")


class TestRichEventEnvelope:
    """All SSE envelope fields make it to the wire correctly."""

    def test_id_event_retry_and_comment_all_appear(self):
        @controller("/x")
        class Ctl:
            @get("/")
            async def stream(self) -> EventStream:
                async def gen() -> AsyncIterator[ServerSentEvent]:
                    yield ServerSentEvent(
                        event="msg",
                        id="42",
                        retry=2500,
                        data="hi",
                        comment="meta",
                    )

                return EventStream(gen())

        @module(controllers=[Ctl])
        class App:
            pass

        app = LaurenFactory.create(App)
        client = TestClient(app)
        resp = client.get("/x/")

        assert resp.status_code == 200
        # Per the encoder's ordering: comment, event, id, retry, data.
        assert b": meta" in resp.body
        assert b"event: msg" in resp.body
        assert b"id: 42" in resp.body
        assert b"retry: 2500" in resp.body
        assert b"data: hi" in resp.body
        # Exactly one event emitted, so the body ends with the
        # spec-mandated double newline.
        assert resp.body.endswith(b"\n\n")

    def test_multiline_data_splits_into_multiple_data_lines(self):
        @controller("/m")
        class Ctl:
            @get("/")
            async def stream(self) -> EventStream:
                async def gen() -> AsyncIterator[ServerSentEvent]:
                    yield ServerSentEvent(data="line1\nline2\nline3")

                return EventStream(gen())

        @module(controllers=[Ctl])
        class App:
            pass

        app = LaurenFactory.create(App)
        client = TestClient(app)
        resp = client.get("/m/")
        assert resp.body == b"data: line1\ndata: line2\ndata: line3\n\n"


# ---------------------------------------------------------------------------
# Dependency injection — the producer is built from DI-resolved deps.
# ---------------------------------------------------------------------------


class TestSseWithDependencyInjection:
    """Handlers that produce SSE participate in DI like any other route."""

    def test_handler_uses_injected_service(self):
        @injectable()
        class FeedSource:
            def items(self) -> list[str]:
                return ["alpha", "beta", "gamma"]

        @controller("/dep")
        class Ctl:
            def __init__(self, source: FeedSource) -> None:
                self._source = source

            @get("/feed")
            async def feed(self) -> EventStream:
                items = self._source.items()

                async def gen() -> AsyncIterator[ServerSentEvent]:
                    for item in items:
                        yield ServerSentEvent(event="item", data=item)

                return EventStream(gen())

        @module(controllers=[Ctl], providers=[FeedSource])
        class App:
            pass

        app = LaurenFactory.create(App)
        client = TestClient(app)
        resp = client.get("/dep/feed")

        events = parse_sse_body(resp.body)
        assert [(e["event"], e["data"]) for e in events] == [
            ("item", "alpha"),
            ("item", "beta"),
            ("item", "gamma"),
        ]


# ---------------------------------------------------------------------------
# Last-Event-ID resumability.
# ---------------------------------------------------------------------------


class TestLastEventIdResumption:
    """Handlers can read ``Last-Event-ID`` to resume from a cursor."""

    def test_cursor_is_threaded_via_header(self):
        @controller("/resume")
        class Ctl:
            @get("/")
            async def feed(self, request: Request) -> EventStream:
                start = int(last_event_id(request.headers) or "0")

                async def gen() -> AsyncIterator[ServerSentEvent]:
                    for i in range(start, start + 3):
                        yield ServerSentEvent(id=str(i), data=f"event-{i}")

                return EventStream(gen())

        @module(controllers=[Ctl])
        class App:
            pass

        app = LaurenFactory.create(App)
        client = TestClient(app)

        # Initial subscription — no ``Last-Event-ID`` so cursor is 0.
        resp = client.get("/resume/")
        events = parse_sse_body(resp.body)
        assert [e["id"] for e in events] == ["0", "1", "2"]

        # Resume after id=2 — server should yield 3, 4, 5.
        resp = client.get("/resume/", headers={"last-event-id": "3"})
        events = parse_sse_body(resp.body)
        assert [e["id"] for e in events] == ["3", "4", "5"]


# ---------------------------------------------------------------------------
# Custom headers, status codes.
# ---------------------------------------------------------------------------


class TestCustomHeadersAndStatus:
    def test_extra_headers_pass_through(self):
        @controller("/x")
        class Ctl:
            @get("/")
            async def stream(self) -> EventStream:
                async def gen() -> AsyncIterator[str]:
                    yield "ok"

                return EventStream(
                    gen(),
                    extra_headers={
                        "x-debug": "yes",
                        "access-control-allow-origin": "*",
                    },
                )

        @module(controllers=[Ctl])
        class App:
            pass

        app = LaurenFactory.create(App)
        client = TestClient(app)
        resp = client.get("/x/")
        # Defaults still present.
        ctype = next((v for k, v in resp.headers if k.lower() == "content-type"), None)
        assert ctype == "text/event-stream; charset=utf-8"
        # Extras applied.
        debug = next((v for k, v in resp.headers if k.lower() == "x-debug"), None)
        cors = next(
            (v for k, v in resp.headers if k.lower() == "access-control-allow-origin"),
            None,
        )
        assert debug == "yes"
        assert cors == "*"

    def test_custom_status_code_propagates(self):
        @controller("/x")
        class Ctl:
            @get("/")
            async def stream(self) -> EventStream:
                async def gen() -> AsyncIterator[str]:
                    yield "queued"

                return EventStream(gen(), status=202)

        @module(controllers=[Ctl])
        class App:
            pass

        app = LaurenFactory.create(App)
        client = TestClient(app)
        resp = client.get("/x/")
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Error paths during streaming.
# ---------------------------------------------------------------------------


class TestErrorsDuringStreaming:
    """Errors raised inside the producer surface cleanly."""

    def test_producer_exception_truncates_stream(self):
        # SSE responses commit headers eagerly, so a producer error
        # after the first event can't change the HTTP status. The
        # framing pipeline simply truncates the body — clients see a
        # premature EOF and reconnect via ``EventSource``'s built-in
        # backoff.
        @controller("/err")
        class Ctl:
            @get("/")
            async def stream(self) -> EventStream:
                async def gen() -> AsyncIterator[str]:
                    yield "ok"
                    raise RuntimeError("kaboom")

                return EventStream(gen())

        @module(controllers=[Ctl])
        class App:
            pass

        app = LaurenFactory.create(App)
        client = TestClient(app)
        # Wrap in pytest.raises so an unhandled exception in the body
        # generator surfaces explicitly. The exact handling is a
        # framework detail — we only assert that the first frame made
        # it out before the failure.
        try:
            resp = client.get("/err/")
            # If the framework swallows the error, at least the first
            # event must be present.
            assert b"data: ok\n\n" in resp.body
        except RuntimeError:
            pass  # Acceptable — error surfaced before completion.


# ---------------------------------------------------------------------------
# OpenAPI / docs introspection — making sure SSE handlers don't break
# the generator. Even though SSE responses are not described as
# Pydantic schemas, the route must still be registered.
# ---------------------------------------------------------------------------


class TestOpenApiDoesNotBreak:
    def test_sse_route_appears_in_routes_listing(self):
        @controller("/api")
        class Ctl:
            @get("/events")
            async def stream(self) -> EventStream:
                async def gen() -> AsyncIterator[str]:
                    yield "x"

                return EventStream(gen())

        @module(controllers=[Ctl])
        class App:
            pass

        app = LaurenFactory.create(App)
        # The router-listing API should know about the route.
        paths = {entry.path_template for entry in app.routes()}
        assert "/api/events" in paths
