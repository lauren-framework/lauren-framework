"""Integration tests for LaurenApp.mount() — ASGI sub-application routing."""

from __future__ import annotations

import json


from lauren import LaurenFactory, controller, get, module
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helpers: tiny ASGI apps used as sub-applications
# ---------------------------------------------------------------------------


class RecordingApp:
    """Records the ASGI scope it was called with and returns 200."""

    __test__ = False  # not a pytest test class

    def __init__(self, status: int = 200, body: bytes = b"ok"):
        self.calls: list[dict] = []
        self._status = status
        self._body = body

    async def __call__(self, scope, receive, send):
        self.calls.append(dict(scope))
        await send(
            {
                "type": "http.response.start",
                "status": self._status,
                "headers": [[b"content-type", b"text/plain"]],
            }
        )
        await send({"type": "http.response.body", "body": self._body})


async def _echo_path_app(scope, receive, send):
    """Return JSON with the received path and root_path."""
    payload = json.dumps({"path": scope["path"], "root_path": scope.get("root_path", "")}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"application/json"]],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _not_found_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b""})


# ---------------------------------------------------------------------------
# Minimal Lauren app fixture
# ---------------------------------------------------------------------------


@controller("/api")
class _ApiCtrl:
    @get("/hello")
    async def hello(self) -> dict:
        return {"from": "lauren"}


@controller("/")
class _HomeCtrl:
    @get("/")
    async def index(self) -> dict:
        return {"home": True}


@module(controllers=[_ApiCtrl, _HomeCtrl])
class _AppMod: ...


def _build_client(**kw) -> TestClient:
    return TestClient(LaurenFactory.create(_AppMod, **kw))


# ---------------------------------------------------------------------------
# Basic HTTP routing through mounts
# ---------------------------------------------------------------------------


class TestMountHTTPRouting:
    def test_mounted_app_receives_request(self):
        recording = RecordingApp()
        client = _build_client()
        client._app.mount("/sub", recording)
        client.get("/sub/anything")
        assert len(recording.calls) == 1

    def test_mounted_app_receives_stripped_path(self):
        client = _build_client()
        client._app.mount("/sub", _echo_path_app)
        resp = client.get("/sub/page")
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == "/page"

    def test_mounted_app_receives_root_path_set(self):
        client = _build_client()
        client._app.mount("/sub", _echo_path_app)
        resp = client.get("/sub/page")
        data = resp.json()
        assert data["root_path"] == "/sub"

    def test_exact_prefix_match_strips_to_root(self):
        """GET /sub with no trailing slash → sub-app receives /."""
        client = _build_client()
        client._app.mount("/sub", _echo_path_app)
        resp = client.get("/sub")
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == "/"

    def test_non_matching_path_reaches_lauren(self):
        client = _build_client()
        client._app.mount("/sub", _not_found_app)
        resp = client.get("/api/hello")
        assert resp.status_code == 200
        assert resp.json()["from"] == "lauren"

    def test_home_route_unaffected_by_mount(self):
        client = _build_client()
        client._app.mount("/sub", _not_found_app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json()["home"] is True

    def test_sub_app_returns_its_status_code(self):
        client = _build_client()
        client._app.mount("/sub", RecordingApp(status=418, body=b"teapot"))
        resp = client.get("/sub/brew")
        assert resp.status_code == 418
        assert resp.body == b"teapot"

    def test_mounted_app_receives_full_sub_path(self):
        client = _build_client()
        client._app.mount("/api/v2", _echo_path_app)
        resp = client.get("/api/v2/users/42")
        data = resp.json()
        assert data["path"] == "/users/42"
        assert data["root_path"] == "/api/v2"


# ---------------------------------------------------------------------------
# Prefix specificity: longest prefix wins
# ---------------------------------------------------------------------------


class TestMountSpecificity:
    def test_longer_prefix_wins_over_shorter(self):
        short = RecordingApp(body=b"short")
        long_ = RecordingApp(body=b"long")
        client = _build_client()
        client._app.mount("/a", short)
        client._app.mount("/a/b", long_)
        resp = client.get("/a/b/something")
        assert resp.body == b"long"
        assert len(short.calls) == 0

    def test_shorter_prefix_used_when_path_does_not_match_longer(self):
        short = RecordingApp(body=b"short")
        long_ = RecordingApp(body=b"long")
        client = _build_client()
        client._app.mount("/a", short)
        client._app.mount("/a/b", long_)
        resp = client.get("/a/c")
        assert resp.body == b"short"
        assert len(long_.calls) == 0

    def test_mount_order_does_not_matter_longest_wins(self):
        """Even if /a/b/c is registered after /a, it must still win."""
        short = RecordingApp(body=b"short")
        long_ = RecordingApp(body=b"long")
        client = _build_client()
        # Register shorter first
        client._app.mount("/a", short)
        client._app.mount("/a/b/c", long_)
        resp = client.get("/a/b/c/deeper")
        assert resp.body == b"long"


# ---------------------------------------------------------------------------
# LaurenFactory.create(mounts=...) convenience
# ---------------------------------------------------------------------------


class TestFactoryMountsIntegration:
    def test_factory_mounts_dict_routes_correctly(self):
        recording = RecordingApp()
        app = LaurenFactory.create(_AppMod, mounts={"/ext": recording})
        client = TestClient(app)
        client.get("/ext/resource")
        assert len(recording.calls) == 1
        assert recording.calls[0]["path"] == "/resource"

    def test_factory_mounts_multiple_sub_apps(self):
        rec_a = RecordingApp(body=b"a")
        rec_b = RecordingApp(body=b"b")
        app = LaurenFactory.create(_AppMod, mounts={"/a": rec_a, "/b": rec_b})
        client = TestClient(app)
        assert client.get("/a/x").body == b"a"
        assert client.get("/b/x").body == b"b"

    def test_factory_mounts_does_not_shadow_lauren_routes(self):
        recording = RecordingApp()
        app = LaurenFactory.create(_AppMod, mounts={"/other": recording})
        client = TestClient(app)
        resp = client.get("/api/hello")
        assert resp.status_code == 200
        assert resp.json()["from"] == "lauren"
        assert len(recording.calls) == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestMountEdgeCases:
    def test_mount_after_factory_works_same_as_before(self):
        recording = RecordingApp()
        app = LaurenFactory.create(_AppMod)
        app.mount("/late", recording)
        client = TestClient(app)
        client.get("/late/x")
        assert len(recording.calls) == 1

    def test_prefix_not_substring_of_unrelated_path(self):
        """'/sub' must not match '/submit'."""
        recording = RecordingApp()
        client = _build_client()
        # No /submit route exists in lauren either, so expect 404 from lauren
        client._app.mount("/sub", recording)
        client.get("/submit")
        assert len(recording.calls) == 0

    def test_deep_nested_path_forwarded_correctly(self):
        client = _build_client()
        client._app.mount("/v1", _echo_path_app)
        resp = client.get("/v1/a/b/c/d")
        data = resp.json()
        assert data["path"] == "/a/b/c/d"
        assert data["root_path"] == "/v1"

    def test_multiple_mounts_same_length_both_reachable(self):
        rec_x = RecordingApp(body=b"x")
        rec_y = RecordingApp(body=b"y")
        client = _build_client()
        client._app.mount("/x", rec_x)
        client._app.mount("/y", rec_y)
        assert client.get("/x/r").body == b"x"
        assert client.get("/y/r").body == b"y"

    def test_mount_with_query_string_passes_through(self):
        """Query string must be preserved in the scope (sub-app reads it)."""
        recording = RecordingApp()
        client = _build_client()
        client._app.mount("/sub", recording)
        client.get("/sub/page?foo=bar")
        call_scope = recording.calls[0]
        assert b"foo=bar" in call_scope.get("query_string", b"")

    def test_root_path_accumulates_when_pre_existing(self):
        """If scope already has a root_path, the prefix is appended."""
        recording = RecordingApp()
        # Build the app and call __call__ directly with an existing root_path
        import asyncio

        app = LaurenFactory.create(_AppMod)
        app.mount("/sub", recording)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/sub/page",
            "query_string": b"",
            "headers": [],
            "root_path": "/myapp",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            pass

        asyncio.run(app(scope, receive, send))
        assert recording.calls[0]["root_path"] == "/myapp/sub"

    def test_unmounted_path_returns_404_from_lauren(self):
        client = _build_client()
        client._app.mount("/sub", RecordingApp())
        resp = client.get("/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Lifespan is never forwarded to mounted apps
# ---------------------------------------------------------------------------


class TestMountLifespan:
    def test_lifespan_not_forwarded_to_sub_app(self):
        """Lifespan events belong exclusively to the lauren app."""
        recording = RecordingApp()
        app = LaurenFactory.create(_AppMod, mounts={"/sub": recording})

        import asyncio

        startup_handled = False

        async def drive_lifespan():
            nonlocal startup_handled
            scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
            events = [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
            idx = 0

            async def receive():
                nonlocal idx
                e = events[idx]
                idx += 1
                return e

            sent = []

            async def send(msg):
                sent.append(msg)

            await app(scope, receive, send)
            return sent

        sent = asyncio.run(drive_lifespan())
        # Lauren should handle lifespan — sub-app never called
        assert len(recording.calls) == 0
        assert any(m.get("type") == "lifespan.startup.complete" for m in sent)
