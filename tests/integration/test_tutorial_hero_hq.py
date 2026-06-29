"""Verifies the docs Tutorial's companion app ("Hero HQ").

The tutorial pages under ``docs/tutorial/`` mirror this app's code; this
suite drives the real package so the tutorial's promises (the curl outputs,
the 422, the 404 envelope, the badge guard, the session round-trip, the SSE
feed, the dispatch task, and the WebSocket broadcast) can never silently rot.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

# The companion app lives beside the tutorial pages (kept out of the built
# site via mkdocs `exclude_docs`). Put its parent on the path so it imports
# as the `hero_hq` package, exactly as the tutorial tells readers to run it.
_TUTORIAL_DIR = pathlib.Path(__file__).resolve().parents[2] / "docs" / "tutorial"
if str(_TUTORIAL_DIR) not in sys.path:
    sys.path.insert(0, str(_TUTORIAL_DIR))

pytestmark = pytest.mark.pydantic  # the dossier models use Pydantic

from hero_hq.main import build_app  # noqa: E402
from lauren.testing import TestClient, WsTestClient  # noqa: E402

BADGE = {"X-HQ-Badge": "hq-badge-007"}
COOKIE = "lauren_session"


def _client() -> TestClient:
    return TestClient(build_app())


def _recruit(client: TestClient, **overrides):
    payload = {"name": "Volt", "power": "lightning", "wattage": 9001}
    payload.update(overrides)
    return client.post("/heroes/", json=payload, headers=BADGE)


def _cookie(resp, name: str = COOKIE) -> str | None:
    sc = resp.header("set-cookie") or ""
    for part in sc.split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part.split("=", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Steps 1-4 — routing, validation, DI, modules
# ---------------------------------------------------------------------------


class TestHeroHQBasics:
    def test_recruit_then_fetch(self):
        client = _client()
        r = _recruit(client)
        assert r.status_code == 201
        hero = r.json()
        assert hero == {"id": 1, "name": "Volt", "power": "lightning", "wattage": 9001}
        got = client.get(f"/heroes/{hero['id']}")
        assert got.status_code == 200
        assert got.json()["name"] == "Volt"

    def test_validation_rejects_bad_paperwork(self):
        # With a valid badge, the Auditor still rejects a non-int wattage.
        client = _client()
        r = _recruit(client, wattage="over nine thousand")
        assert r.status_code == 422

    def test_missing_hero_returns_404_envelope(self):
        client = _client()
        r = client.get("/heroes/999")
        assert r.status_code == 404
        assert r.json() == {
            "error": {"code": "hero_not_found", "message": "no such hero", "detail": {"id": 999}}
        }

    def test_roster_lists_recruited_heroes(self):
        client = _client()
        _recruit(client, name="Volt")
        _recruit(client, name="Tide", power="water", wattage=4200)
        names = [h["name"] for h in client.get("/heroes/").json()]
        assert names == ["Volt", "Tide"]

    def test_di_singleton_is_shared_across_requests(self):
        client = _client()
        rid = _recruit(client).json()["id"]
        assert client.get(f"/heroes/{rid}").json()["name"] == "Volt"

    def test_path_param_must_be_int(self):
        client = _client()
        assert client.get("/heroes/not-a-number").status_code == 422

    def test_module_boundary_is_wired(self):
        assert build_app() is not None


# ---------------------------------------------------------------------------
# Step 5 — the Door Bouncer (guards + custom error)
# ---------------------------------------------------------------------------


class TestBadgeGuard:
    def test_recruit_without_badge_is_403(self):
        client = _client()
        r = client.post("/heroes/", json={"name": "Volt", "power": "lightning", "wattage": 9001})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "villain_detected"

    def test_recruit_with_badge_is_201(self):
        assert _recruit(_client()).status_code == 201

    def test_reading_the_roster_is_public(self):
        # GET routes are unguarded — anyone at HQ can browse the roster.
        assert _client().get("/heroes/").status_code == 200


# ---------------------------------------------------------------------------
# Step 6 — sessions (who are you, really?)
# ---------------------------------------------------------------------------


class TestSessions:
    def test_anonymous_visitor_is_unidentified(self):
        assert _client().get("/me/").json() == {"identified": False}

    def test_login_then_whoami(self):
        client = _client()
        hero_id = _recruit(client, name="Volt").json()["id"]
        login = client.post("/me/login", json={"hero_id": hero_id})
        assert login.json() == {"welcome": "Volt"}
        sid = _cookie(login)
        assert sid is not None
        who = client.get("/me/", cookies={COOKIE: sid})
        assert who.json() == {"identified": True, "hero_id": hero_id, "name": "Volt"}

    def test_logout_clears_identity(self):
        client = _client()
        hero_id = _recruit(client).json()["id"]
        sid = _cookie(client.post("/me/login", json={"hero_id": hero_id}))
        out = client.post("/me/logout", cookies={COOKIE: sid})
        assert out.json() == {"farewell": True}
        assert "Max-Age=0" in (out.header("set-cookie") or "")
        # The old cookie no longer identifies anyone.
        assert client.get("/me/", cookies={COOKIE: sid}).json() == {"identified": False}

    def test_login_as_unknown_hero_is_401(self):
        client = _client()
        r = client.post("/me/login", json={"hero_id": 999})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Step 7 — Mission Control (SSE + background task + WebSocket)
# ---------------------------------------------------------------------------


class TestMissionControl:
    def test_sse_feed_emits_status_then_close(self):
        body = _client().get("/missions/feed").body
        assert b"event: status" in body
        assert b"all quiet on sector 1" in body
        assert b"event: close" in body
        assert b"stand down" in body

    def test_dispatch_is_202_and_runs_after_response(self):
        client = _client()
        r = client.post("/missions/dispatch", params={"hero": "Volt"})
        assert r.status_code == 202
        assert r.json() == {"dispatched": "Volt"}
        # The background task has already run by the time the response returns.
        assert client.get("/missions/log").json() == {"dispatched": ["Volt"]}

    def test_comms_broadcasts_to_every_connected_hero(self):
        app = build_app()

        async def run():
            client = WsTestClient(app)
            async with client.connect("/comms") as ws1:
                async with client.connect("/comms") as ws2:
                    await ws1.send_json({"event": "chat", "text": "assemble!"})
                    m1 = await asyncio.wait_for(ws1.receive_json(), timeout=2.0)
                    m2 = await asyncio.wait_for(ws2.receive_json(), timeout=2.0)
                    assert m1 == {"chat": "assemble!"}
                    assert m2 == {"chat": "assemble!"}

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Step 9 — operations (lifecycle hooks + OpenAPI)
# ---------------------------------------------------------------------------


class TestOperations:
    def test_status_reports_grid_online_after_startup(self):
        # @post_construct ran during TestClient startup, so the grid is online.
        assert _client().get("/status/").json() == {"hq": "online", "grid": True}

    def test_openapi_document_is_served(self):
        doc = _client().get("/openapi.json")
        assert doc.status_code == 200
        assert "/heroes/" in str(doc.json().get("paths", {}))

    def test_pre_destruct_powers_the_grid_down(self):
        # Build, start, then shut down — @pre_destruct flips the grid offline.
        app = build_app()
        client = TestClient(app)
        assert client.get("/status/").json()["grid"] is True
        asyncio.run(app.shutdown())
        from hero_hq.operations import PowerGrid

        grid = asyncio.run(app.container.resolve(PowerGrid))
        assert grid.online is False
