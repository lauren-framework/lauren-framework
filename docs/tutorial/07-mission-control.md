# 7. Mission Control, Live

> The heroes are recruited, badged, and logged in. They're standing around HQ drinking
> coffee, waiting for something to do. Let's give them a live-ops room: a status feed that
> streams, a dispatch button that doesn't make anyone wait, and a comms channel everyone
> hears. Welcome to Mission Control.

!!! abstract "📋 Mission briefing"
    **You'll build:** a Server-Sent Events feed, a fire-and-forget dispatch task, and a
    WebSocket comms channel.
    **You'll learn:**

    - [ ] Streaming with `EventStream` / `ServerSentEvent`
    - [ ] Deferring work with `BackgroundTasks` (respond now, work later)
    - [ ] Real-time `@ws_controller` gateways and `BroadcastGroup` rooms

We'll build all three in one file, `hero_hq/mission_control.py`.

---

## A status feed that streams

A handler that returns an `EventStream` streams events to the client as they're produced —
perfect for a live status board. The browser's built-in `EventSource` even reconnects for
free.

```python title="hero_hq/mission_control.py"
from lauren import (
    BackgroundTasks,
    EventStream,
    Json,
    Scope,
    ServerSentEvent,
    controller,
    get,
    injectable,
    post,
)
from lauren.websockets import (
    BroadcastGroup,
    WebSocket,
    on_connect,
    on_disconnect,
    on_message,
    ws_controller,
)


@injectable(scope=Scope.SINGLETON)
class MissionLog:
    """A shared log of who's been dispatched (one for the whole app)."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def record(self, hero: str) -> None:
        self.dispatched.append(hero)


@controller("/missions")
class MissionControlController:
    def __init__(self, log: MissionLog) -> None:
        self.log = log

    @get("/feed")
    async def feed(self) -> EventStream:
        async def producer():
            for sector in range(1, 4):
                yield ServerSentEvent(event="status", data=f"all quiet on sector {sector}")
            yield ServerSentEvent(event="close", data="stand down")

        return EventStream(producer())
```

Each `yield` is framed per the SSE spec and flushed to the client immediately. Our producer
finishes after a few events; a real feed would loop forever, `await`-ing new updates.

---

## Dispatch without the wait

Scrambling the quinjet takes a moment, and the dispatcher shouldn't have to stare at a
spinner while it happens. Declare a `BackgroundTasks` parameter, queue the slow work, and
return a `202 Accepted` **now** — Lauren runs the task *after* the response is sent.

```python title="hero_hq/mission_control.py (continued)"
    @post("/dispatch")
    async def dispatch(self, hero: str, tasks: BackgroundTasks) -> tuple[dict, int]:
        # Fire-and-forget: respond now (202), scramble the quinjet after.
        tasks.add_task(self.log.record, hero)
        return {"dispatched": hero}, 202

    @get("/log")
    async def log_view(self) -> dict:
        return {"dispatched": self.log.dispatched}
```

!!! danger "💥 Villainous Pitfall"
    Capture **plain values** (or singletons) in `add_task`, never a request-scoped object.
    Request-scoped instances are torn down the instant the response goes out — your task
    would reach for the Sidekick and find he's already gone home. Here `self.log` is a
    singleton, so it's safe.

---

## A comms channel everyone hears

For two-way, real-time chatter, reach for a WebSocket gateway. `@ws_controller` is the WS
analogue of `@controller`; `@on_connect` / `@on_message` / `@on_disconnect` are its hooks.
A `BroadcastGroup` manages rooms — subscribe connections, then `broadcast` to all of them.

```python title="hero_hq/mission_control.py (continued)"
@injectable(scope=Scope.SINGLETON)
class CommsRoom(BroadcastGroup):
    """HQ's comms channel. BroadcastGroup isn't injectable itself — subclass it."""


@ws_controller("/comms")
class CommsGateway:
    def __init__(self, room: CommsRoom) -> None:
        self.room = room

    @on_connect
    async def connect(self, ws: WebSocket) -> None:
        # Join the HQ channel. Every connected hero hears every broadcast.
        await self.room.subscribe("hq", ws)

    @on_message("chat")
    async def chat(self, ws: WebSocket, body: Json[dict]) -> None:
        await self.room.broadcast("hq", {"chat": body.get("text", "")})

    @on_disconnect
    async def disconnect(self, ws: WebSocket) -> None:
        await self.room.unsubscribe_all(ws)
```

A client sends `{"event": "chat", "text": "..."}`; the `event` field routes it to the
matching `@on_message("chat")` handler, and the broadcast fans out to everyone in the `"hq"`
room — including the sender.

!!! tip "⚡ Hero Tip"
    `BroadcastGroup` itself is not injectable — you **subclass** it and decorate the subclass
    with `@injectable`. That's how you can run several independent channels (comms, alerts,
    telemetry) as distinct singletons.

---

## Add Mission Control to HQ

Give it a team — note it provides both the `MissionLog` and the `CommsRoom` — and slot it
into the root module:

```python title="hero_hq/teams.py" hl_lines="3 4 5 6 9"
@module(
    controllers=[MissionControlController, CommsGateway],
    providers=[MissionLog, CommsRoom],
)
class MissionControlModule:
    """The live-ops room — SSE feed, dispatch tasks, and team comms."""


@module(imports=[DispatchModule, IdentityModule, MissionControlModule])
class HeroHQModule:
    """All of Hero HQ, assembled."""
```

---

## ✅ Checkpoint

```text
hero_hq/
├── models.py
├── roster.py
├── security.py
├── dispatch.py
├── auth.py
├── mission_control.py   # SSE feed + dispatch task + comms gateway  ← new
├── teams.py             # + MissionControlModule
└── main.py
```

!!! example "🧪 Try it"
    ```bash
    # The streaming feed (curl prints each event as it arrives):
    $ curl -N localhost:8000/missions/feed
    event: status
    data: all quiet on sector 1
    ...
    event: close
    data: stand down

    # Dispatch returns instantly with 202; the work runs after:
    $ curl -i -X POST 'localhost:8000/missions/dispatch?hero=Volt'
    HTTP/1.1 202 Accepted
    {"dispatched":"Volt"}

    $ curl localhost:8000/missions/log
    {"dispatched":["Volt"]}
    ```

    For the WebSocket, point any client at `ws://localhost:8000/comms`, send
    `{"event":"chat","text":"assemble!"}`, and watch every connected client receive
    `{"chat":"assemble!"}`.

**What changed:** Hero HQ now streams live status, dispatches without blocking, and runs a
real-time comms channel — the full real-time toolkit, in one module.

---

🎉 **You built Hero HQ.** You started with an empty office and ended with a validated,
dependency-injected, multi-module, badge-guarded, session-aware, real-time API — and a test
suite proving it works.

**Next:** Steps 8–9 (testing the whole thing properly and shipping it to production) are on
their way.
**Go deeper:** [Server-Sent Events](../guides/server-sent-events.md) ·
[Typed Streaming](../guides/typed-streaming.md) · [WebSockets](../guides/websockets.md) ·
[Background Tasks](../guides/background-tasks.md)
