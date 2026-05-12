"""Unit tests for LaurenApp.mount()."""

from __future__ import annotations

import pytest

from lauren import LaurenFactory, controller, get, module


# ---------------------------------------------------------------------------
# Minimal app fixture
# ---------------------------------------------------------------------------


@controller("/")
class _RootCtrl:
    @get("/ping")
    async def ping(self) -> dict:
        return {"pong": True}


@module(controllers=[_RootCtrl])
class _AppMod: ...


def _app():
    return LaurenFactory.create(_AppMod)


# ---------------------------------------------------------------------------
# Tiny ASGI app helpers
# ---------------------------------------------------------------------------


async def _echo_app(scope, receive, send):
    """Return 200 with path + root_path as JSON."""
    body = (f'{{"path": "{scope["path"]}", "root_path": "{scope.get("root_path", "")}"}}').encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"application/json"]],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _always_404(scope, receive, send):
    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b""})


# ---------------------------------------------------------------------------
# mount() method API
# ---------------------------------------------------------------------------


class TestMountMethod:
    def test_mount_adds_to_mounts_list(self):
        app = _app()
        assert app.mounts == []
        app.mount("/sub", _echo_app)
        assert len(app.mounts) == 1
        prefix, sub = app.mounts[0]
        assert prefix == "/sub"
        assert sub is _echo_app

    def test_mount_normalises_trailing_slash(self):
        app = _app()
        app.mount("/sub/", _echo_app)
        prefix, _ = app.mounts[0]
        assert prefix == "/sub"

    def test_mount_adds_leading_slash_if_missing(self):
        app = _app()
        app.mount("sub", _echo_app)
        prefix, _ = app.mounts[0]
        assert prefix == "/sub"

    def test_mount_empty_path_raises(self):
        app = _app()
        with pytest.raises(ValueError, match="must not be empty"):
            app.mount("", _echo_app)

    def test_mount_returns_none(self):
        app = _app()
        result = app.mount("/x", _echo_app)
        assert result is None

    def test_multiple_mounts_sorted_longest_first(self):
        app = _app()
        app.mount("/a", _echo_app)
        app.mount("/a/b/c", _echo_app)
        app.mount("/a/b", _echo_app)
        prefixes = [p for p, _ in app.mounts]
        assert prefixes == ["/a/b/c", "/a/b", "/a"]

    def test_mounts_property_is_snapshot(self):
        """Mutating the returned list must not affect internal state."""
        app = _app()
        app.mount("/x", _echo_app)
        snap = app.mounts
        snap.append(("/bogus", _echo_app))
        assert len(app.mounts) == 1


# ---------------------------------------------------------------------------
# LaurenFactory.create(mounts=...)
# ---------------------------------------------------------------------------


class TestFactoryMountsParam:
    def test_factory_mounts_dict_applied(self):
        app = LaurenFactory.create(_AppMod, mounts={"/sub": _echo_app})
        prefixes = [p for p, _ in app.mounts]
        assert "/sub" in prefixes

    def test_factory_mounts_none_is_noop(self):
        app = LaurenFactory.create(_AppMod, mounts=None)
        assert app.mounts == []

    def test_factory_mounts_empty_dict_is_noop(self):
        app = LaurenFactory.create(_AppMod, mounts={})
        assert app.mounts == []

    def test_factory_mounts_multiple(self):
        app = LaurenFactory.create(
            _AppMod,
            mounts={"/a": _echo_app, "/b": _always_404},
        )
        prefixes = [p for p, _ in app.mounts]
        assert "/a" in prefixes
        assert "/b" in prefixes
