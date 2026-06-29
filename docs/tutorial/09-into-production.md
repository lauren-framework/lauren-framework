# 9. Into Production

> The suite is green, the heroes are restless, and the city isn't going to save itself. Time
> to ship Hero HQ — with the lights coming on cleanly at startup, going off cleanly at
> shutdown, and a front door for the API docs. The goal: when the pager goes off at 3 a.m.,
> the framework is the *boring* part of the story.

!!! abstract "📋 Mission briefing"
    **You'll build:** lifecycle hooks, a health check, and a production run command.
    **You'll learn:**

    - [ ] `@post_construct` / `@pre_destruct` for startup warm-up and clean shutdown
    - [ ] Graceful shutdown on `SIGTERM` / `SIGINT`
    - [ ] The built-in OpenAPI docs, and serving with a real ASGI server

---

## Turn the lights on (and off) cleanly

Remember Captain Singleton from step 3? Let's give him the HQ power grid — a singleton that
warms up when the app starts and shuts down cleanly when it stops. `@post_construct` runs once
at startup; `@pre_destruct` runs once during graceful shutdown, with a bounded timeout.

```python title="hero_hq/operations.py"
--8<-- "docs/tutorial/hero_hq/operations.py:code"
```

Give it a team and slot it into HQ:

```python title="hero_hq/teams.py" hl_lines="1 2 5"
@module(controllers=[StatusController], providers=[PowerGrid])
class OperationsModule:
    """The power grid — lifecycle hooks and a health check."""


@module(imports=[DispatchModule, IdentityModule, MissionControlModule, OperationsModule])
class HeroHQModule:
    """All of Hero HQ, assembled."""
```

Now `GET /status/` is a real health check your load balancer can poll — and it reports `true`
only after `@post_construct` has run.

!!! tip "⚡ Hero Tip"
    Lifecycle hooks run in **dependency order** at startup and **reverse** order at shutdown,
    each with a timeout. A blocking sync hook is offloaded to a thread so it can't wedge the
    event loop. Put "open the database pool" in `@post_construct` and "close it" in
    `@pre_destruct`, and Lauren handles the ordering.

---

## Shut down like a professional

When a deploy sends `SIGTERM`, you want in-flight rescues to finish, `@pre_destruct` hooks to
run, and *then* the process to exit — not a hard kill mid-mission. One function call wires
that up:

```python title="hero_hq/main.py (production entry)"
from lauren.signals import install_signal_handlers

app = build_app()
install_signal_handlers(app)  # SIGTERM / SIGINT → graceful drain → @pre_destruct → exit
```

Need to run your own cleanup that isn't tied to a provider? Register it directly:

```python
app.on_shutdown(lambda: print("HQ going dark — good night"))
```

---

## The front door for the docs

You enabled this back in step 4 with `docs_url="/docs"` and `openapi_url="/openapi.json"`.
Lauren generates an OpenAPI 3.1 document from your models, extractors, and routes — no
annotations required — and serves interactive docs:

!!! example "🧪 Try it"
    ```bash
    $ curl localhost:8000/status/
    {"hq":"online","grid":true}

    # The machine-readable contract:
    $ curl -s localhost:8000/openapi.json | head -c 80
    {"openapi":"3.1.0","info":{...}}
    ```

    Then open **`http://localhost:8000/docs`** in a browser for Swagger UI — every route, the
    `CreateHero` / `HeroOut` schemas, and the `villain_detected` / `hero_not_found` error
    codes, all documented automatically.

---

## Send the heroes into the field

Lauren is a standard ASGI app, so deploy it like any other — pick a server and scale with
workers:

```bash
# Uvicorn (great default)
uvicorn hero_hq.main:app --host 0.0.0.0 --port 8000 --workers 4

# Granian (Rust-based, fast on CPython)
granian --interface asgi hero_hq.main:app --host 0.0.0.0 --port 8000

# Hypercorn (HTTP/2 + HTTP/3)
hypercorn hero_hq.main:app --bind 0.0.0.0:8000
```

!!! danger "💥 Villainous Pitfall"
    With multiple workers, your `SINGLETON`s are per-*worker*, not per-*cluster*. The
    in-memory `HeroRepository`, `MissionLog`, and the default session store each live in one
    process. For real multi-worker production, back them with something shared — Redis,
    Postgres — behind the same interfaces. (The tutorial's in-memory versions are perfect for
    one worker and for tests; villains, sadly, scale horizontally too.)

---

## ✅ Checkpoint

```text
hero_hq/
├── models.py
├── roster.py
├── security.py
├── dispatch.py
├── auth.py
├── mission_control.py
├── operations.py    # PowerGrid lifecycle + /status health check  ← new
├── teams.py         # + OperationsModule
└── main.py          # install_signal_handlers(app)
```

**What changed:** HQ now warms up and shuts down cleanly, exposes a health check and
auto-generated API docs, and runs under a production ASGI server. Ship it.

---

🎓 **You did it.** From an empty office to a deployed, tested, real-time API — validation,
dependency injection, modules, guards, sessions, streaming, and graceful lifecycle, all
composed into one coherent app. The city is in good hands.

**Next:** [Where to go from here →](whats-next.md)
**Go deeper:** [Lifecycle Hooks](../core-concepts/lifecycle.md) ·
[Signals & Lifecycle Events](../guides/signals.md)
