"""Mission Control — SSE feed, a dispatch task, and team comms (tutorial step 7)."""

from __future__ import annotations

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


@injectable(scope=Scope.SINGLETON)
class CommsRoom(BroadcastGroup):
    """HQ's comms channel. BroadcastGroup isn't injectable itself — subclass it."""


@controller("/missions")
class MissionControlController:
    def __init__(self, log: MissionLog) -> None:
        self.log = log

    @get("/feed")
    async def feed(self) -> EventStream:
        # A live status feed. The browser's EventSource reconnects for free.
        async def producer():
            for sector in range(1, 4):
                yield ServerSentEvent(event="status", data=f"all quiet on sector {sector}")
            yield ServerSentEvent(event="close", data="stand down")

        return EventStream(producer())

    @post("/dispatch")
    async def dispatch(self, hero: str, tasks: BackgroundTasks) -> tuple[dict, int]:
        # Fire-and-forget: respond now (202), scramble the quinjet after.
        tasks.add_task(self.log.record, hero)
        return {"dispatched": hero}, 202

    @get("/log")
    async def log_view(self) -> dict:
        return {"dispatched": self.log.dispatched}


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
