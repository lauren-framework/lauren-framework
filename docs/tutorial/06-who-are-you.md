# 6. Who Are You, Really?

> A badge gets you through the door, but the moment a hero walks to the next desk, HQ has
> forgotten them entirely. ("Name? Power? Have we met?") That's no way to run an agency.
> HQ needs to *remember* who's logged in — across requests. That's a **session**.

!!! abstract "📋 Mission briefing"
    **You'll build:** hero login, "who am I?", and logout — backed by a signed-cookie session.
    **You'll learn:**

    - [ ] Turning on sessions with one factory kwarg
    - [ ] Reading and writing `session: Session` in a handler
    - [ ] `regenerate_id()` at login (the fixation defence) and `invalidate()` at logout

---

## Switch on HQ's memory

Sessions are a first-class feature: pass a `SessionConfig` to the factory and every request
gets a (signed, `HttpOnly`) session cookie. The cookie is **signed, not encrypted** — so a
client can't forge it, but don't store secrets in it.

```python title="hero_hq/main.py" hl_lines="3 5 11 12 13 14"
from lauren import LaurenFactory, SessionConfig

from .teams import HeroHQModule

# In production, load the secret from the environment — never hardcode it.
SESSION_SECRET = "change-me-hero-hq-secret-please-32-bytes"


def build_app():
    return LaurenFactory.create(
        HeroHQModule,
        sessions=SessionConfig(
            secret=SESSION_SECRET,
            secure=False,  # local dev is HTTP; flip to True (the default) in production
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )


app = build_app()
```

!!! danger "💥 Villainous Pitfall"
    `secure=True` (the default) tells the browser to only send the cookie over HTTPS — so on
    a plain `http://localhost` dev server your login would silently never stick. We set
    `secure=False` **for local dev only**. In production, leave it `True`. Future-you, paged
    at 3 a.m., will be grateful.

---

## The badge office

Now a controller where heroes log in. Inject `session: Session` into any handler — Lauren
provides it at zero per-request cost, just like `request`. It behaves like a `dict`:

```python title="hero_hq/auth.py"
--8<-- "docs/tutorial/hero_hq/auth.py:code"
```

Two security habits worth forming, both one line:

- **`session.regenerate_id()` at login.** This issues a fresh session id whenever the trust
  level changes, which shuts down *session fixation* attacks. Make it a reflex.
- **`session.invalidate()` at logout.** Clears the session and expires the cookie.

Lauren only writes the cookie back when the session actually changed — a plain read costs you
nothing on the response.

Finally, give the badge office its own team and add it to HQ:

```python title="hero_hq/teams.py" hl_lines="3 6 7 10"
# ... RosterModule and DispatchModule as before ...

@module(controllers=[IdentityController], imports=[RosterModule])
class IdentityModule:
    """The badge office — hero login / logout via sessions."""


@module(imports=[DispatchModule, IdentityModule])  # MissionControl joins in step 7
class HeroHQModule:
    """All of Hero HQ, assembled."""
```

`HeroHQModule` is the new top-level team that assembles the others; point
`build_app()` at it (we already did, above).

---

## ✅ Checkpoint

```text
hero_hq/
├── models.py
├── roster.py
├── security.py
├── dispatch.py
├── auth.py        # IdentityController (login / whoami / logout)  ← new
├── teams.py       # + IdentityModule, + HeroHQModule root
└── main.py        # sessions=SessionConfig(...)
```

!!! example "🧪 Try it"
    `TestClient` (and your browser) thread the cookie automatically; with raw `curl` you pass
    it back yourself via `-b`:

    ```bash
    # Before login, HQ doesn't know you:
    $ curl localhost:8000/me/
    {"identified":false}

    # Log in as hero #1 — note the Set-Cookie, and save the jar:
    $ curl -c jar.txt -X POST localhost:8000/me/login \
        -H 'Content-Type: application/json' -d '{"hero_id":1}'
    {"welcome":"Volt"}

    # Now HQ remembers you:
    $ curl -b jar.txt localhost:8000/me/
    {"identified":true,"hero_id":1,"name":"Volt"}
    ```

**What changed:** HQ now remembers who's logged in across requests, with secure session
cookies and a one-line fixation defence.

---

**Next:** [7. Mission Control, Live →](07-mission-control.md) — heroes are recruited, badged,
and logged in. Time to actually dispatch them — in real time.
**Go deeper:** [Sessions](../guides/sessions.md)
