"""Target module for the in-process ``python -m lauren`` tests (``test_cli_runner.py``)."""

from __future__ import annotations

import asyncio
import signal
from typing import Any

from lauren import Lauren, LaurenApp, LaurenFactory, Scope, injectable, module, post_construct, pre_destruct
from lauren.commands import CommandContext, command

EVENTS: list[str] = []


@injectable()
class Database:
    @post_construct
    def open(self) -> None:
        EVENTS.append("db.open")

    @pre_destruct
    def close(self) -> None:
        EVENTS.append("db.close")


@injectable(scope=Scope.REQUEST)
class Session:
    async def aclose(self) -> None:
        EVENTS.append("session.close")


@injectable()
class Commands:
    def __init__(self, db: Database) -> None:
        self.db = db

    @command("greet", aliases=("hello",))
    async def greet(self, ctx: CommandContext, name: str = "world", *, shout: bool = False) -> None:
        """Say hello."""
        text = f"hello {name}"
        ctx.echo(text.upper() if shout else text)

    @command("add")
    def add(self, a: int, b: int) -> int:
        return a + b

    @command("info")
    def info(self) -> dict[str, Any]:
        return {"ok": True}

    @command("boom")
    async def boom(self, session: Session) -> None:
        raise RuntimeError("kaboom")

    @command("interrupt")
    async def interrupt(self, session: Session, *, which: str = "SIGINT") -> None:
        EVENTS.append("interrupt.raise")
        signal.raise_signal(getattr(signal, which))
        try:
            await asyncio.sleep(30)
        finally:
            EVENTS.append("interrupt.finally")

    @command("survive")
    async def survive(self) -> int:
        signal.raise_signal(signal.SIGINT)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return 0  # swallowing the cancellation still exits 130
        return 5  # pragma: no cover

    @command("confirm")
    async def confirm(self, ctx: CommandContext) -> int:
        return 0 if ctx.confirm("Sure?") else 1

    @command("secret", hidden=True)
    def secret(self) -> str:
        return "hidden"


@module(providers=[Database, Session, Commands])
class AppModule:
    pass


def create_app() -> LaurenApp:
    return LaurenFactory.create(AppModule)


async def acreate_app() -> LaurenApp:
    return LaurenFactory.create(AppModule)


def make_lauren() -> Lauren:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_module(AppModule)
    return app


@injectable()
class Broken:
    @post_construct
    def open(self) -> None:
        raise RuntimeError("cannot connect")


@injectable()
class Commandless:
    @command("noop")
    def noop(self) -> None: ...


@module(providers=[Broken, Commandless])
class BrokenStartupModule:
    pass


class Unregistered:
    pass


@injectable()
class NeedsMissing:
    def __init__(self, dep: Unregistered) -> None:
        self.dep = dep


@module(providers=[NeedsMissing])
class BrokenGraphModule:
    pass


def broken_graph_app() -> LaurenApp:
    return LaurenFactory.create(BrokenGraphModule)


@injectable()
class SlowStart:
    @post_construct
    async def open(self) -> None:
        EVENTS.append("slow.open")
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(30)


@module(providers=[SlowStart, Commandless, Database])
class InterruptedStartupModule:
    pass


@module(providers=[Database])
class EmptyModule:
    pass


class NotAModule:
    pass
