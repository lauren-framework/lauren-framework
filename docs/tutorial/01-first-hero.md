# 1. Recruit Your First Hero

> Every agency starts with an empty office and a phone that isn't ringing yet. Let's get
> Hero HQ's front desk online.

!!! abstract "📋 Mission briefing"
    **You'll build:** a one-route Lauren app you can hit with `curl`.
    **You'll learn:**

    - [ ] Installing Lauren and an ASGI server
    - [ ] What `@controller` and `@get` actually do
    - [ ] Wiring a controller into a `@module` and the `LaurenFactory`

---

## Set up the office

Lauren is a standard ASGI app, so you need Lauren plus a server to run it. We'll use
`uvicorn`, and `pydantic` (the Auditor will want it in step 2):

```bash
pip install "lauren[pydantic]" "uvicorn[standard]"
```

Make a project folder. Through this tutorial we'll keep everything in a package called
`hero_hq`:

```text
hero_hq/
└── main.py
```

---

## Open the front desk

A **controller** is a class that handles HTTP requests. You mark it with `@controller(prefix)`,
and each method becomes a route when you decorate it with a verb like `@get` or `@post`.
Decorators in Lauren only *attach metadata* — they never rewrite your function, so what you
write is what runs.

```python title="hero_hq/main.py"
from lauren import LaurenFactory, controller, get, module


@controller("/heroes")
class HeroController:
    @get("/")
    async def index(self) -> dict:
        return {"hq": "Hero HQ", "status": "open for business"}


@module(controllers=[HeroController])
class AppModule:
    pass


app = LaurenFactory.create(AppModule)
```

Three things just happened:

- `@controller("/heroes")` says "every route in this class lives under `/heroes`."
- `@get("/")` turns `index` into a handler for `GET /heroes/`. Returning a `dict` is fine —
  Lauren serializes it to JSON for you.
- `@module(controllers=[HeroController])` groups your controllers (and later, providers)
  into a unit. `LaurenFactory.create(AppModule)` compiles the whole thing into an immutable
  app at startup — and `app` is now a plain ASGI callable.

!!! tip "⚡ Hero Tip"
    `LaurenFactory.create(...)` does all its work **once, at startup**. If you misspell a
    route, depend on something that doesn't exist, or wire a module wrong, it raises right
    here — not at 3 a.m. when a hero is mid-rescue. Failing fast is a feature.

---

## Turn on the lights

```bash
uvicorn hero_hq.main:app --reload
```

!!! example "🧪 Try it"
    ```bash
    $ curl localhost:8000/heroes/
    {"hq":"Hero HQ","status":"open for business"}
    ```

That's a fully-routed, ASGI-served HTTP API. It doesn't do much yet — the phone is on but
nobody's been hired — so let's start taking applications.

---

## ✅ Checkpoint

You have a running Lauren app with one controller and one route:

```text
hero_hq/
└── main.py        # HeroController + AppModule + app
```

**What changed:** there was no app; now there's a front desk answering `GET /heroes/`.

---

**Next:** [2. The Hero Dossier →](02-hero-dossier.md) — let heroes actually apply, and meet
the Auditor.
**Go deeper:** [Controllers](../core-concepts/controllers.md) · [Modules](../core-concepts/modules.md)
