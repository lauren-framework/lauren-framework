# 5. The Bouncer at the Door

> Word got out that HQ is recruiting, and now a suspicious figure in an obviously-fake
> mustache is trying to add himself to the roster as "Definitely Not A Villain, wattage:
> 999999." Time to put someone on the door.

!!! abstract "📋 Mission briefing"
    **You'll build:** a `BadgeGuard` that protects the recruit endpoint, and a custom error.
    **You'll learn:**

    - [ ] What a **guard** is and how `can_activate` decides allow/deny
    - [ ] Attaching a guard to a route with `@use_guards`
    - [ ] Raising a custom `HTTPError` with a stable code and envelope

---

## Hire a bouncer

A **guard** runs *before* a handler and decides whether the request gets through. It's a
class with one async method, `can_activate`, that returns `True` to allow — or raises to
reject with a specific error.

Our bouncer checks for an HQ badge in a header. No valid badge, no entry — and the Villain
gets a `403` with a stable error code your frontend can branch on:

```python title="hero_hq/security.py"
--8<-- "docs/tutorial/hero_hq/security.py:code"
```

The guard receives an `ExecutionContext` — a small bundle with the request and the
handler's metadata. Here we only need `ctx.request.headers`.

---

## Put the bouncer on the recruit door

Reading the roster is public — anyone at HQ can browse heroes. But *recruiting* changes
data, so we guard just that route with `@use_guards`:

```python title="hero_hq/dispatch.py" hl_lines="1 5 12"
from lauren import Json, Path, controller, get, post, use_guards

from .models import CreateHero, HeroOut
from .roster import HeroRepository
from .security import BadgeGuard

# ... HeroNotFoundError and the rest of HeroController unchanged ...

    @post("/")
    @use_guards(BadgeGuard)
    async def recruit(self, body: Json[CreateHero]) -> tuple[HeroOut, int]:
        # Recruiting changes the roster, so the Door Bouncer checks a badge first.
        hero = self.roster.recruit(body.name, body.power, body.wattage)
        return HeroOut(**hero), 201
```

That's the whole change: import the guard, stack `@use_guards(BadgeGuard)` on the method.
You don't register the guard anywhere — Lauren sees it referenced and wires it into the DI
container at startup.

!!! tip "⚡ Hero Tip"
    Guards run **before** body extraction. A request with no badge is bounced before the
    Auditor ever reads the body — so you never waste validation on a villain.

!!! danger "💥 Villainous Pitfall"
    A guard that returns `False` gets you a generic `403 Forbidden`. If you want a *specific*
    message and code (so your frontend can tell "bad badge" from "rate-limited"), **raise**
    your own `HTTPError` subclass instead of returning `False`. The Villain deserves a
    personalized rejection.

---

## ✅ Checkpoint

```text
hero_hq/
├── models.py
├── roster.py
├── security.py    # BadgeGuard + VillainDetectedError  ← new
├── dispatch.py    # recruit now @use_guards(BadgeGuard)
├── teams.py
└── main.py
```

!!! example "🧪 Try it"
    ```bash
    # No badge — the Bouncer bounces you, with your custom envelope:
    $ curl -i -X POST localhost:8000/heroes/ -H 'Content-Type: application/json' \
        -d '{"name":"Definitely Not A Villain","power":"trickery","wattage":999999}'
    HTTP/1.1 403 Forbidden
    {"error":{"code":"villain_detected","message":"halt! that is not a valid HQ badge","detail":{"hint":"send a valid X-HQ-Badge header"}}}

    # With a badge — welcome aboard:
    $ curl -X POST localhost:8000/heroes/ \
        -H 'Content-Type: application/json' -H 'X-HQ-Badge: hq-badge-007' \
        -d '{"name":"Volt","power":"lightning","wattage":9001}'
    {"id":1,"name":"Volt","power":"lightning","wattage":9001}

    # Reading is still public — no badge needed:
    $ curl localhost:8000/heroes/
    [{"id":1,"name":"Volt","power":"lightning","wattage":9001}]
    ```

**What changed:** the recruit endpoint is now badge-protected, and unauthorized requests get
a clean, codified `403`.

---

**Next:** [6. Who Are You, Really? →](06-who-are-you.md) — badges get you in the door, but HQ
should remember who you are between requests. Time for sessions.
**Go deeper:** [Custom Guards](../guides/custom-guards.md) ·
[Guard vs middleware vs interceptor](../concepts/extractors-vs-dependencies-vs-guards-vs-middlewares.md)
