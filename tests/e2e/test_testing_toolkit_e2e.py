"""End-to-end: one lifespan-managed client drives a multi-module app with overrides.

The app has a loop-bound "database" singleton, core sessions, an upload
endpoint, an SSE feed and a WebSocket gateway; the testing module swaps the
"external" mail service and the database for in-memory fakes.
"""

from __future__ import annotations

import asyncio

from lauren import (
    EventStream,
    Json,
    ServerSentEvent,
    Session,
    SessionConfig,
    UploadFile,
    WebSocket,
    controller,
    get,
    injectable,
    module,
    on_connect,
    on_message,
    post,
    post_construct,
    pre_destruct,
    ws_controller,
)
from lauren.testing import Test, TestClient


@injectable()
class Database:
    """Real database: would open a network connection at startup."""

    @post_construct
    async def connect(self) -> None:
        raise ConnectionError("no database in tests")  # pragma: no cover - always overridden


class FakeDatabase:
    """Loop-bound fake: an asyncio.Lock and a queue created at startup."""

    @post_construct
    async def connect(self) -> None:
        self.lock = asyncio.Lock()
        self.rows: list[str] = []
        self.feed: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    @pre_destruct
    async def close(self) -> None:
        self.closed = True

    async def insert(self, row: str) -> None:
        async with self.lock:
            self.rows.append(row)
            await self.feed.put(row)


@injectable()
class Mailer:
    def send(self, to: str) -> None:  # pragma: no cover - always overridden
        raise ConnectionError("no SMTP in tests")


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, to: str) -> None:
        self.sent.append(to)


@module(providers=[Database, Mailer], exports=[Database, Mailer])
class InfraModule:
    pass


@controller("/api")
class ApiController:
    def __init__(self, db: Database, mailer: Mailer) -> None:
        self.db = db
        self.mailer = mailer

    @post("/login")
    async def login(self, session: Session, body: Json[dict]) -> dict:
        session.regenerate_id()
        session["user"] = body["user"]
        self.mailer.send(body["user"])
        return {"ok": True}

    @get("/me")
    async def me(self, session: Session) -> dict:
        return {"user": session.get("user")}

    @post("/upload")
    async def upload(self, file: UploadFile) -> dict:
        await self.db.insert(file.filename or "?")  # type: ignore[attr-defined]
        return {"stored": file.filename}

    @get("/feed")
    async def feed(self) -> EventStream:
        async def produce():
            while True:
                row = await self.db.feed.get()  # type: ignore[attr-defined]
                yield ServerSentEvent(data=row, event="row")

        return EventStream(produce())


@ws_controller("/ws/rows")
class RowsGateway:
    def __init__(self, db: Database) -> None:
        self.db = db

    @on_connect
    async def joined(self, ws: WebSocket) -> None:
        await ws.accept()

    @on_message("count")
    async def count(self, ws: WebSocket) -> None:
        await ws.send_json({"event": "count", "data": len(self.db.rows)})  # type: ignore[attr-defined]


@module(imports=[InfraModule], controllers=[ApiController, RowsGateway])
class AppModule:
    pass


def test_full_toolkit_round_trip() -> None:
    mailer = FakeMailer()
    app = (
        Test.create_testing_module(AppModule, sessions=SessionConfig(secret="k" * 32))
        .override_provider(Database)
        .use_class(FakeDatabase)
        .override_provider(Mailer)
        .use_value(mailer)
        .compile()
    )
    with TestClient(app) as client:
        # Sessions through the cookie jar.
        client.post("/api/login", json={"user": "ada"})
        assert client.get("/api/me").json() == {"user": "ada"}
        assert mailer.sent == ["ada"]

        # Uploads land in the loop-bound fake database...
        with client.stream("GET", "/api/feed") as feed:
            assert client.post("/api/upload", files={"file": ("a.csv", b"1,2", "text/csv")}).json() == {
                "stored": "a.csv"
            }
            event = next(feed.iter_sse())
        # ...and stream out over SSE on the same event loop.
        assert (event.event, event.data) == ("row", "a.csv")

        # The WebSocket gateway sees the same singleton.
        with client.websocket_connect("/ws/rows") as ws:
            ws.send_event("count")
            assert ws.receive_event() == {"event": "count", "data": 1}

        db = client.resolve(Database)
        assert isinstance(db, FakeDatabase) and db.closed is False
    assert db.closed is True
