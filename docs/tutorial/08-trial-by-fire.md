# 8. Trial by Fire

> HQ's lawyers ("Cape & Cowl LLP") won't let a single hero into the field until the backend
> is tested. They are, annoyingly, correct. Good news: Lauren ships an in-process test client,
> so you can put the whole agency through its paces without a real server or a real socket.

!!! abstract "📋 Mission briefing"
    **You'll build:** a test suite that exercises routes, the guard, sessions, SSE, and comms.
    **You'll learn:**

    - [ ] Driving the app with `TestClient` — no network required
    - [ ] Testing guards, validation, and error envelopes
    - [ ] Threading session cookies and testing WebSockets with `WsTestClient`

---

## The in-process test client

`TestClient` runs your app through the ASGI protocol directly. It even runs startup for you,
so lifecycle hooks fire just like in production. Create one per app:

```python title="tests/test_hero_hq.py"
from lauren.testing import TestClient

from hero_hq.main import build_app

BADGE = {"X-HQ-Badge": "hq-badge-007"}


def client() -> TestClient:
    return TestClient(build_app())


def test_recruit_then_fetch():
    c = client()
    r = c.post("/heroes/", json={"name": "Volt", "power": "lightning", "wattage": 9001}, headers=BADGE)
    assert r.status_code == 201
    hero = r.json()
    assert c.get(f"/heroes/{hero['id']}").json()["name"] == "Volt"


def test_missing_hero_is_a_clean_404():
    r = client().get("/heroes/999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "hero_not_found"
```

Each `build_app()` is a fresh, isolated app — no shared state leaks between tests.

---

## Test the bouncer and the Auditor

A good suite proves the doors are locked *and* that the right people get in:

```python title="tests/test_hero_hq.py (continued)"
def test_recruiting_without_a_badge_is_403():
    r = client().post("/heroes/", json={"name": "Sneaky", "power": "?", "wattage": 1})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "villain_detected"


def test_the_auditor_rejects_bad_paperwork():
    r = client().post(
        "/heroes/", json={"name": "Volt", "power": "lightning", "wattage": "lots"}, headers=BADGE
    )
    assert r.status_code == 422
```

!!! tip "⚡ Hero Tip"
    Test the *unhappy* paths first — the `403`, the `422`, the `404`. Anyone can make the
    happy path work; the bugs that page you at 3 a.m. live in the rejections.

---

## Test sessions: thread the cookie by hand

`TestClient` keeps no cookie jar, which is a feature for tests — you control exactly what's
sent. Pull the cookie out of one response and pass it back on the next:

```python title="tests/test_hero_hq.py (continued)"
def _session_cookie(resp) -> str | None:
    sc = resp.header("set-cookie") or ""
    for part in sc.split(";"):
        if part.strip().startswith("lauren_session="):
            return part.strip().split("=", 1)[1]
    return None


def test_login_is_remembered():
    c = client()
    hero_id = c.post(
        "/heroes/", json={"name": "Volt", "power": "lightning", "wattage": 9001}, headers=BADGE
    ).json()["id"]

    sid = _session_cookie(c.post("/me/login", json={"hero_id": hero_id}))
    who = c.get("/me/", cookies={"lauren_session": sid})
    assert who.json() == {"identified": True, "hero_id": hero_id, "name": "Volt"}
```

---

## Test the live stuff

The SSE feed is buffered by `TestClient`, so you can assert on the whole body. WebSockets get
their own `WsTestClient`:

```python title="tests/test_hero_hq.py (continued)"
import asyncio

from lauren.testing import WsTestClient


def test_status_feed_streams_then_closes():
    body = client().get("/missions/feed").body
    assert b"event: status" in body
    assert b"event: close" in body


def test_comms_broadcasts_to_everyone():
    app = build_app()

    async def run():
        ws_client = WsTestClient(app)
        async with ws_client.connect("/comms") as a, ws_client.connect("/comms") as b:
            await a.send_json({"event": "chat", "text": "assemble!"})
            assert await a.receive_json() == {"chat": "assemble!"}
            assert await b.receive_json() == {"chat": "assemble!"}

    asyncio.run(run())
```

!!! danger "💥 Villainous Pitfall"
    Don't reuse one app across two `TestClient`s if you rely on a clean slate — share state
    only when you mean to. For isolated tests, `build_app()` per test keeps each trial
    hermetic. (HQ's lawyers love the word "hermetic.")

---

## ✅ Checkpoint

```text
tests/
└── test_hero_hq.py    # routes, guard, validation, sessions, SSE, comms
```

Run it:

```bash
pytest -q
```

**What changed:** every promise the API makes is now backed by a test — including the
rejections. Cape & Cowl LLP is, grudgingly, satisfied.

---

**Next:** [9. Into Production →](09-into-production.md) — the suite's green. Time to ship HQ
without the pager going off.
**Go deeper:** [Testing skill](https://github.com/lauren-framework/lauren-framework/tree/main/skills/testing-lauren-apps) ·
[`lauren.testing` reference](../reference/testing.md)
