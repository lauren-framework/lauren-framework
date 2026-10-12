"""A minimal, dependency-free ASGI lifespan driver.

``httpx.ASGITransport`` speaks HTTP and nothing else: it never sends the
``lifespan`` scope, so it never tells an application that it has "started".
A real ASGI server (uvicorn, hypercorn, granian) always drives lifespan startup
*before* the first request, so a framework is entitled to do its boot work
there.

Sanic is the one framework in this field that takes that entitlement: without a
lifespan handshake its first request fails with ``SanicException: Loop can only
be retrieved after the app has started running``.  Measuring Sanic without
lifespan would therefore measure a configuration no user ever deploys — and
would have produced a "Sanic is not drivable in-process" verdict that is simply
wrong.

Rather than special-case Sanic, the harness drives lifespan for *every*
framework, because that is what the deployed path looks like.  Applications
that ignore lifespan (most of them) are unaffected; applications that need it
get it.  When an application neither answers nor crashes — some ASGI objects
simply ignore a lifespan scope — the driver gives up quietly and the
measurement proceeds, with ``supported`` set to ``False`` so the reason is
reportable rather than invisible.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from types import TracebackType
from typing import Any, Self

__all__ = ["DEFAULT_TIMEOUT_S", "LifespanRunner", "LifespanStartupError"]

logger = logging.getLogger(__name__)

#: How long to wait for a lifespan response before assuming it is unsupported.
DEFAULT_TIMEOUT_S = 20.0

_LIFESPAN_SCOPE: dict[str, Any] = {
    "type": "lifespan",
    "asgi": {"version": "3.0", "spec_version": "2.0"},
}


class LifespanStartupError(RuntimeError):
    """The application explicitly reported ``lifespan.startup.failed``."""


class LifespanRunner:
    """Drive ASGI lifespan startup/shutdown for one application instance.

    Use it around the requests you want to measure::

        async with LifespanRunner(app) as lifespan:
            if not lifespan.supported:
                ...  # the app ignored lifespan; that is fine
            await client.get("/")
    """

    def __init__(self, app: Any, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._app = app
        self._timeout = timeout
        self._receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

        self.supported = True
        """False when the application ignored or refused the lifespan scope."""

        self.note = ""
        """Human-readable reason when :attr:`supported` is False."""

    async def _receive(self) -> dict[str, Any]:
        return await self._receive_queue.get()

    async def _send(self, message: dict[str, Any]) -> None:
        await self._send_queue.put(message)

    async def startup(self) -> None:
        """Send ``lifespan.startup`` and wait for the application to answer."""
        try:
            coro = self._app(_LIFESPAN_SCOPE, self._receive, self._send)
        except TypeError as exc:
            # The object is not an ASGI callable at all — the caller will hit
            # the same TypeError on its request and can report it there.
            self.supported = False
            self.note = f"app object is not ASGI-callable ({type(exc).__name__}: {exc})"
            return

        self._task = asyncio.create_task(coro)
        await self._receive_queue.put({"type": "lifespan.startup"})

        getter: asyncio.Task[dict[str, Any]] = asyncio.create_task(self._send_queue.get())
        done, _ = await asyncio.wait(
            {self._task, getter},
            timeout=self._timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if getter in done:
            message = getter.result()
            if message["type"] == "lifespan.startup.failed":
                await self._abandon("")
                raise LifespanStartupError(message.get("message", "lifespan startup failed"))
            return

        getter.cancel()
        if self._task in done:
            error = self._task.exception()
            await self._abandon(f"app exited without answering lifespan startup ({error!r})")
        else:
            await self._abandon(f"no lifespan startup response within {self._timeout:g}s")

    async def _abandon(self, note: str) -> None:
        """Give up on lifespan, leaving the application usable if it is."""
        self.supported = False
        self.note = note
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def shutdown(self) -> None:
        """Send ``lifespan.shutdown`` and let the application finish."""
        if self._task is None:
            return
        await self._receive_queue.put({"type": "lifespan.shutdown"})
        getter: asyncio.Task[dict[str, Any]] = asyncio.create_task(self._send_queue.get())
        try:
            await asyncio.wait(
                {self._task, getter},
                timeout=self._timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if getter.done():
                getter.result()
        except Exception:
            logger.debug("lifespan shutdown raised for %r", self._app, exc_info=True)
        finally:
            getter.cancel()
            if not self._task.done():
                self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def __aenter__(self) -> Self:
        await self.startup()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.shutdown()
