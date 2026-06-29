<!--
  Tutorial tone guide (for contributors):
  - Humor lives in prose, callouts, and names — NEVER in code behaviour.
    Every code block must run exactly as written.
  - ~1 wink per ~150 words. Density turns charming into exhausting.
  - Never trade clarity for a bit. If the gag obscures the concept, cut it.
  - Keep the recurring cast (Captain Singleton, the Sidekick, the Door
    Bouncer, the Auditor, the Villain, Mission Control). No fresh in-jokes.
  - The reader is the competent hero; the bureaucracy and villains are the butt.
  The canonical, tested source for every snippet is docs/tutorial/hero_hq/,
  exercised by tests/integration/test_tutorial_hero_hq.py.
-->
# Tutorial: Build Hero HQ

> You've just been hired as the backend engineer for **Hero HQ**, a staffing agency that
> dispatches superheroes to city-scale emergencies. The heroes are powerful but allergic
> to paperwork. Your job: build the API that keeps the cape-wearing chaos organized — and
> learn Lauren, end to end, while you do it.

This is the **guided, build-one-app** path through Lauren. Where the
[Quickstart](../getting-started/quickstart.md) is a five-minute sprint to see the shape of
a Lauren app, the tutorial is the afternoon project: you start from `pip install` and end
with a validated, dependency-injected, guarded, session-aware, real-time, **tested** API,
adding exactly one concept per step.

!!! tip "⚡ How to follow along"
    Type the code, don't just read it — heroism is a contact sport. Each step ends with a
    **✅ Checkpoint** showing the app's state, so if something drifts you can diff against
    it. The finished app lives in [`docs/tutorial/hero_hq/`](https://github.com/lauren-framework/lauren-framework/tree/main/docs/tutorial/hero_hq)
    and is covered by the test suite, so every snippet here is real, running code.

## What you'll build

A backend for dispatching heroes: recruit them, validate their (frankly improbable)
paperwork, store them on a shared roster, organize them into teams, keep villains out,
remember who's logged in, stream live mission updates, and ship the whole thing to
production without the pager going off.

## Meet the cast

Lauren's concepts are easier to remember when they have a face. You'll work with:

| Character | Who they are | What they teach |
|---|---|---|
| **Captain Singleton** | There is exactly one of him, ever. Runs the HQ power grid. | `Scope.SINGLETON` |
| **The Sidekick** | A fresh one shows up per mission and goes home after. | `Scope.REQUEST` |
| **The Auditor** | Rejects malformed hero paperwork with a withering `422`. | Validation / extractors |
| **The Door Bouncer** | Checks badges. Bounces villains. | Guards / auth |
| **The Villain** | Exists mainly to trip your error envelopes. | `HTTPError` subclasses |
| **Mission Control** | The live-ops desk that never sleeps. | SSE / WebSockets / tasks |

## The steps

<div class="grid cards" markdown>

-   :material-account-plus: [__1. Recruit Your First Hero__](01-first-hero.md)

    ---
    Install Lauren, write one controller, and serve your first route. Hello, hero.

-   :material-file-document-edit: [__2. The Hero Dossier__](02-hero-dossier.md)

    ---
    Typed request bodies and path params, automatic validation, and the Auditor's `422`s.

-   :material-database: [__3. The HQ Roster__](03-the-roster.md)

    ---
    Dependency injection and scopes — Captain Singleton's shared roster vs. the Sidekick.

-   :material-account-group: [__4. Assembling Teams__](04-assembling-teams.md)

    ---
    Modules with `imports`/`exports`. A `@module` is literally a superhero team.

-   :material-shield-account: [__5. The Bouncer at the Door__](05-the-bouncer.md)

    ---
    Guards and custom errors — keep villains off the roster with a badge check.

-   :material-card-account-details: [__6. Who Are You, Really?__](06-who-are-you.md)

    ---
    Sessions — log heroes in, remember them across requests, log them out.

-   :material-radio-tower: [__7. Mission Control, Live__](07-mission-control.md)

    ---
    Real-time: an SSE status feed, fire-and-forget dispatch tasks, and WebSocket comms.

-   :material-test-tube: [__8. Trial by Fire__](08-trial-by-fire.md)

    ---
    Test the whole agency in-process with `TestClient` and `WsTestClient` — including the rejections.

-   :material-rocket-launch: [__9. Into Production__](09-into-production.md)

    ---
    Lifecycle hooks, graceful shutdown, OpenAPI docs, and a real ASGI deploy.

-   :material-flag-checkered: [__What's Next__](whats-next.md)

    ---
    Where to go from here — Core Concepts, Guides, Reference, and taking Hero HQ further.

</div>

!!! success "🎓 The full journey"
    All nine steps are here — from `pip install` to a deployed, tested, real-time API.
    Follow them in order, or jump to the one you need.

**Prerequisites:** Python 3.11+, a terminal, and the willingness to name variables after
superheroes. No prior Lauren knowledge required.

Ready? [Recruit your first hero →](01-first-hero.md)
