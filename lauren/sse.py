"""Server-Sent Events primitives — :class:`ServerSentEvent`, :class:`EventStream`.

Server-Sent Events (`HTML Living Standard § 9.2 \\
<https://html.spec.whatwg.org/multipage/server-sent-events.html>`_) are a
one-way streaming protocol layered on plain HTTP. They give browsers a
``text/event-stream`` feed they can consume with ``new EventSource(url)``,
which automatically reconnects on transport errors and forwards a
``Last-Event-ID`` header for resumability.

This module provides the two value types most apps need:

* :class:`ServerSentEvent` — a frozen dataclass that bundles the optional
  spec fields (``event``, ``id``, ``retry``, ``comment``) alongside the
  ``data`` payload. Multiline data and JSON-able payloads are handled
  for you.
* :class:`EventStream` — a :class:`~lauren.types.Response` subclass that
  wraps an async iterable of events and frames each one according to
  the spec, with optional keep-alive heartbeats so intermediaries don't
  close idle connections.

The two are designed to slot directly into lauren's existing handler
return-coercion pipeline. ``EventStream`` IS a ``Response``, so a
handler can simply ``return EventStream(generate())`` and the dispatcher
treats it identically to any other response.

Symmetry with :class:`~lauren.streaming.StreamingResponse`
---------------------------------------------------------

Lauren already supports *typed* streaming via
``-> StreamingResponse[T]``: an async iterable of validated Pydantic
models negotiated to SSE / NDJSON / JSON Lines from the ``Accept``
header. ``EventStream`` is the **untyped** counterpart aimed at use
cases where:

* The event shape is heterogeneous (chat messages, log lines, queue
  notifications mixing several record types).
* You need explicit control of the SSE envelope — ``event:`` names,
  ``id:`` values for resumability, ``retry:`` directives.
* You want a heartbeat to keep proxies from idling out the connection.

Pick :class:`StreamingResponse` when you have a single Pydantic schema;
pick :class:`EventStream` when you want raw SSE control.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Iterable,
    Mapping,
    Union,
)

from .types import Headers, Response

# ---------------------------------------------------------------------------
# Public value type — ServerSentEvent
# ---------------------------------------------------------------------------


#: Type alias used in :class:`EventStream` signatures. A producer may yield
#: either fully-formed :class:`ServerSentEvent` instances, plain strings
#: (treated as ``data``), or dicts (auto-promoted via
#: :meth:`ServerSentEvent.from_dict`).
SSEItem = Union["ServerSentEvent", str, bytes, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    """A single Server-Sent Event with its full envelope.

    Per the HTML spec, only ``data`` is meaningful to clients on its
    own; the other fields are optional dispatch hints:

    * ``event`` becomes ``ev.type`` on the browser side, letting
      ``EventSource.addEventListener("foo", ...)`` route the message.
    * ``id`` is sent back as the ``Last-Event-ID`` header on automatic
      reconnects — the canonical hook for resumable streams.
    * ``retry`` advises the client's reconnect backoff (milliseconds).
    * ``comment`` emits a non-data ``: text\\n\\n`` line, useful for
      keep-alive pings or human-readable transport markers.

    The dataclass is **frozen** because event values flow through
    asyncio queues and broadcast registries where mutability would be a
    correctness hazard.
    """

    #: ``data`` defaults to ``None`` (rather than the empty string) so a
    #: comment-only event (``ServerSentEvent(comment="ping")``) does
    #: NOT emit a stray ``data:`` line. Callers who explicitly want an
    #: empty data field can pass ``data=""``.
    data: Any = None
    event: str | None = None
    id: str | None = None
    retry: int | None = None
    comment: str | None = None

    @classmethod
    def from_dict(cls, mapping: Mapping[str, Any]) -> "ServerSentEvent":
        """Build a :class:`ServerSentEvent` from a plain mapping.

        Used by the framing path so producer generators can yield bare
        ``{"event": "...", "data": "..."}`` dicts without instantiating
        the dataclass themselves. Unknown keys are ignored so callers
        can pass through richer shapes without pre-filtering. Missing
        keys default to ``None`` (matching the dataclass), which keeps
        comment-only and event-only frames from sprouting empty
        ``data:`` lines.
        """
        return cls(
            data=mapping.get("data"),
            event=mapping.get("event"),
            id=mapping.get("id"),
            retry=mapping.get("retry"),
            comment=mapping.get("comment"),
        )

    def encode(self, *, encoder: Any = None) -> bytes:
        """Return the UTF-8 bytes of this event in the SSE wire format.

        The encoded form ends in the spec-mandated double newline
        (``\\n\\n``) that flushes the event on the browser side.
        Multiline data values are split into multiple ``data:`` lines
        per spec; JSON-able non-string payloads are serialized via
        *encoder* (or the active encoder when *encoder* is ``None``).
        """
        return format_sse_event(
            data=self.data,
            event=self.event,
            id=self.id,
            retry=self.retry,
            comment=self.comment,
            encoder=encoder,
        ).encode("utf-8")


# ---------------------------------------------------------------------------
# Wire formatting — isolated for testability
# ---------------------------------------------------------------------------


def format_sse_event(
    *,
    data: Any = None,
    event: str | None = None,
    id: str | None = None,
    retry: int | None = None,
    comment: str | None = None,
    encoder: Any = None,
) -> str:
    """Format a single Server-Sent Event into its on-the-wire string form.

    Layered as a free function so the framing logic is unit-testable
    without a full :class:`ServerSentEvent` round-trip and so other
    callers (the keep-alive task, internal heartbeats) can emit comment
    frames cheaply.

    Spec compliance notes (HTML Living Standard §9.2):

    * Each ``\\n`` inside a ``data`` value MUST become its own
      ``data: ...\\n`` line. We split on ``\\n`` and emit one line per
      segment. Trailing ``\\n`` in the value produces an empty
      ``data:`` line, which is still valid framing.
    * ``id`` MUST NOT contain a newline. We strip them; an alternative
      would be to raise, but silently scrubbing matches the behaviour
      of every server library I've measured (Starlette, Sanic, Flask).
    * ``retry`` MUST be an integer — a non-int value is silently
      omitted (per spec, the browser would discard it anyway).
    * ``comment`` lines start with ``:`` and contain no field name.
    * The terminating blank line (``\\n``) is emitted exactly once at
      the end of the event — we always end with ``\\n\\n``.
    """
    parts: list[str] = []
    if comment is not None:
        for line in str(comment).split("\n"):
            parts.append(f": {line}")
    if event is not None:
        ev = str(event).replace("\n", " ").replace("\r", " ")
        parts.append(f"event: {ev}")
    if id is not None:
        clean_id = str(id).replace("\n", "").replace("\r", "")
        parts.append(f"id: {clean_id}")
    if retry is not None:
        if isinstance(retry, bool):
            # ``bool`` is an ``int`` subclass; explicitly reject it so
            # ``retry=True`` doesn't surface as ``retry: 1``.
            pass
        elif isinstance(retry, int) and retry >= 0:
            parts.append(f"retry: {retry}")

    # Spec rule: each line of ``data`` becomes its own ``data:`` line.
    # ``data=None`` means "omit the data field entirely" (used by
    # comment-only frames); a literal ``data=""`` still emits a valid
    # ``data: \n`` line. ``_encode_data`` handles the distinction.
    encoded_data = _encode_data(data, encoder=encoder)
    if encoded_data is not None:
        for line in encoded_data.split("\n"):
            parts.append(f"data: {line}")

    if not parts:
        # Nothing to emit — yield a single newline so callers can use
        # ``format_sse_event()`` as a no-op heartbeat without raising.
        return "\n"
    return "\n".join(parts) + "\n\n"


def _encode_data(data: Any, *, encoder: Any = None) -> str | None:
    """Return the string form of ``data`` for use in ``data:`` lines.

    * ``None`` returns ``None`` so the caller can omit the ``data:``
      field entirely — useful for comment-only events.
    * ``bytes`` / ``bytearray`` are decoded as UTF-8.
    * ``str`` passes through.
    * Anything else is JSON-encoded via the provided *encoder* (or the
      process-wide active encoder when *encoder* is ``None``) so custom
      backends (orjson, msgspec) are honoured for SSE payloads too.
    """
    if data is None:
        return None
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", errors="replace")
    if hasattr(data, "model_dump") and callable(getattr(data, "model_dump")):
        try:
            data = data.model_dump(mode="json")
        except TypeError:
            pass
    from .serialization import get_active_encoder  # noqa: PLC0415

    _enc = encoder or get_active_encoder()
    return _enc.encode_compact(data).decode("utf-8")


# ---------------------------------------------------------------------------
# EventStream — the Response subclass
# ---------------------------------------------------------------------------


class EventStream(Response):
    """A streaming HTTP response that frames events as Server-Sent Events.

    Usage::

        @get("/notifications")
        async def notifications(self, q: Depends[Queue]) -> EventStream:
            async def producer():
                async for ev in q.subscribe():
                    yield ServerSentEvent(event=ev.kind, data=ev.payload)
            return EventStream(producer(), keep_alive=15.0)

    The wrapped iterable may yield any of the shapes defined by
    :data:`SSEItem`:

    * :class:`ServerSentEvent` — emitted as-is.
    * ``str`` — wrapped in ``ServerSentEvent(data=...)``.
    * ``bytes`` — decoded as UTF-8 and wrapped.
    * ``Mapping`` — promoted via :meth:`ServerSentEvent.from_dict`.
    * any other value — JSON-encoded and wrapped as ``data``.

    Keep-alive
    ----------

    Network intermediaries (load balancers, reverse proxies, mobile
    radios) frequently kill idle connections after 30–60 seconds. Pass
    ``keep_alive=N`` (seconds) to have the response emit a comment
    frame every ``N`` seconds when the producer has nothing to send.
    Comment frames are spec-mandated to be ignored by the browser
    ``EventSource`` consumer, so they keep the connection live without
    polluting the application event stream.

    Headers
    -------

    The response sets:

    * ``Content-Type: text/event-stream; charset=utf-8`` — spec media type.
    * ``Cache-Control: no-cache`` — disables intermediate caching.
    * ``X-Accel-Buffering: no`` — nginx-specific buffering opt-out.
    * ``Connection: keep-alive`` — explicit for older proxies.
    """

    #: Default heartbeat comment text. ``"keep-alive"`` is conventional
    #: across the industry and trivially debuggable in browser devtools.
    DEFAULT_KEEPALIVE_COMMENT = "keep-alive"

    def __init__(
        self,
        iterable: "AsyncIterable[SSEItem] | Iterable[SSEItem]",
        *,
        status: int = 200,
        keep_alive: float | None = None,
        keep_alive_comment: str = DEFAULT_KEEPALIVE_COMMENT,
        extra_headers: "Headers | Mapping[str, str] | Iterable[tuple[str, str]] | None" = None,
        encoder: Any = None,
    ) -> None:
        if keep_alive is not None and keep_alive <= 0:
            raise ValueError(f"keep_alive must be positive when set, got {keep_alive!r}")

        async_iter = _ensure_async_iterable(iterable)
        self._source: AsyncIterable[Any] = async_iter
        self._keep_alive: float | None = keep_alive
        self._keep_alive_comment: str = keep_alive_comment
        self._encoder: Any = encoder

        framed = _frame_event_stream(
            async_iter,
            keep_alive=keep_alive,
            keep_alive_comment=keep_alive_comment,
            encoder=encoder,
        )

        headers = MutableSseHeaders(
            [
                ("content-type", "text/event-stream; charset=utf-8"),
                ("cache-control", "no-cache"),
                ("connection", "keep-alive"),
                ("x-accel-buffering", "no"),
            ]
        )
        if extra_headers is not None:
            if isinstance(extra_headers, Headers):
                items = extra_headers.raw()
            elif isinstance(extra_headers, Mapping):
                items = list(extra_headers.items())
            else:
                items = list(extra_headers)
            for k, v in items:
                headers.set(k.lower(), v)

        super().__init__(
            b"",
            status=status,
            headers=headers,
            media_type="text/event-stream",
            stream=framed,
        )

    def _clone(self, **kwargs: Any) -> "EventStream":  # type: ignore[override]
        """Override to copy EventStream-specific attributes alongside Response state."""
        new = super()._clone(**kwargs)
        new._source = self._source  # type: ignore[attr-defined]
        new._keep_alive = self._keep_alive  # type: ignore[attr-defined]
        new._keep_alive_comment = self._keep_alive_comment  # type: ignore[attr-defined]
        new._encoder = self._encoder  # type: ignore[attr-defined]
        return new  # type: ignore[return-value]

    def _reframe(self, encoder: Any) -> None:
        """Rebuild the stream with *encoder* before the response is sent.

        Called by ``_coerce_to_response`` to inject the app's configured
        encoder into an ``EventStream`` that was constructed by user handler
        code (which has no access to the app-level encoder at that point).
        Safe to call because the async generator hasn't been iterated yet.
        """
        self._stream = _frame_event_stream(
            self._source,
            keep_alive=self._keep_alive,
            keep_alive_comment=self._keep_alive_comment,
            encoder=encoder,
        )
        self._encoder = encoder


class MutableSseHeaders(Headers):
    """Tiny mutable Headers subclass used for SSE response construction.

    ``Headers`` itself is immutable; the existing public ``MutableHeaders``
    subclass is appropriate but introduces a slightly different
    case-folding contract on ``set``. This local subclass keeps the
    surface narrow — only :meth:`set` is needed to merge
    ``extra_headers`` overrides.
    """

    def set(self, key: str, value: str) -> None:
        key_l = key.lower()
        self._items = [(k, v) for k, v in self._items if k != key_l]
        self._items.append((key_l, value))


# ---------------------------------------------------------------------------
# Internal framing pipeline
# ---------------------------------------------------------------------------


async def _frame_event_stream(
    source: AsyncIterable[Any],
    *,
    keep_alive: float | None,
    keep_alive_comment: str,
    encoder: Any = None,
) -> AsyncIterator[bytes]:
    """Yield SSE-formatted frame bytes from the source iterable.

    Layered out of :class:`EventStream` so the framing rules can be
    exercised in isolation. Three responsibilities live here:

    1. **Item normalisation** — every yielded item is funnelled
       through :func:`_coerce_to_event` so the wire format only ever
       sees :class:`ServerSentEvent` instances.
    2. **Frame emission** — each event is encoded once and yielded as
       ``bytes``.
    3. **Keep-alive multiplexing** — when ``keep_alive`` is set the
       routine races each ``__anext__`` against an :func:`asyncio.sleep`
       and emits a comment frame whenever the timer wins. The race uses
       :func:`asyncio.wait` with ``FIRST_COMPLETED`` because cancelling
       a winning task is cheaper than the alternative of building a
       separate timeout coroutine per item.

    Cancellation safety
    -------------------

    When the client disconnects the ASGI server cancels the response
    task; that propagates here as a :class:`asyncio.CancelledError`.
    The pending ``__anext__`` task (and the keep-alive sleep, if any)
    are cancelled in the ``finally`` block so no fleeting tasks leak.
    The user iterator's own ``aclose`` is invoked too, so any context
    manager owned by the producer (DB connections, pubsub
    subscriptions, ...) is released cleanly.
    """
    iterator = source.__aiter__()

    if keep_alive is None:
        # Hot path — no keep-alive multiplexing.
        try:
            async for item in _aiter_events(iterator):
                yield item.encode(encoder=encoder)
        finally:
            await _safe_aclose(iterator)
        return

    # Keep-alive path: race ``__anext__`` against a sleep.
    #
    # The ``pending`` ``__anext__`` task is created **once per iterator
    # advancement** — NOT once per heartbeat. When the sleep wins, we
    # leave ``pending`` untouched so it can finish on a subsequent
    # iteration; only when the iterator actually produces a value (or
    # raises ``StopAsyncIteration``) do we move on to the next item.
    # Restarting ``__anext__`` after each heartbeat would leak the
    # previously-awaited task, which is the bug this refactor fixes.
    pending: "asyncio.Task[Any] | None" = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            sleeper = asyncio.ensure_future(asyncio.sleep(keep_alive))
            try:
                done, _ = await asyncio.wait(  # type: ignore[type-var]
                    {pending, sleeper},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                # Always cancel the sleeper — either it lost the race
                # (we don't need it any more) or we're unwinding due to
                # cancellation (we want to free the timer task).
                if not sleeper.done():
                    sleeper.cancel()
                    try:
                        await sleeper
                    except BaseException:
                        pass
            if pending in done:
                try:
                    raw = pending.result()  # type: ignore[union-attr]
                except StopAsyncIteration:
                    pending = None  # consumed; nothing left to cancel
                    return
                event = _coerce_to_event(raw)
                yield event.encode(encoder=encoder)
                # Schedule the next item read for the next race round.
                pending = asyncio.ensure_future(iterator.__anext__())
            else:
                # Sleep finished first — emit a heartbeat. ``pending``
                # is left in flight; the next loop turn races it again
                # against a fresh sleeper.
                yield format_sse_event(comment=keep_alive_comment).encode("utf-8")
    except asyncio.CancelledError:
        # Standard ASGI cancellation when the client goes away. Re-raise
        # so the framework records the cancellation correctly. The
        # ``finally`` block below cancels the in-flight ``__anext__``.
        raise
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            try:
                await pending
            except BaseException:
                pass
        await _safe_aclose(iterator)


async def _safe_aclose(iterator: AsyncIterator[Any]) -> None:
    """Best-effort ``aclose`` on an iterator, swallowing transport errors.

    Shared by the keep-alive and non-keep-alive paths so user-supplied
    ``aclose`` semantics are consistent across both. Errors raised by
    the iterator's own cleanup are intentionally suppressed — they
    can't change the outcome of the response (which has already been
    framed) and would otherwise mask the original cancellation reason.
    """
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except (asyncio.CancelledError, Exception):
        pass


async def _aiter_events(
    iterator: AsyncIterator[Any],
) -> AsyncIterator[ServerSentEvent]:
    """Adapt the user iterator to a stream of :class:`ServerSentEvent`.

    Centralising the coercion keeps the keep-alive and non-keep-alive
    paths in :func:`_frame_event_stream` symmetric.
    """
    async for raw in iterator:
        yield _coerce_to_event(raw)


def _coerce_to_event(item: Any) -> ServerSentEvent:
    """Normalise an arbitrary producer-yielded value to a ServerSentEvent.

    Accepted shapes mirror :data:`SSEItem` so producer code can stay
    naive (yielding strings, dicts, or models) without ceremony.
    """
    if isinstance(item, ServerSentEvent):
        return item
    if isinstance(item, Mapping):
        return ServerSentEvent.from_dict(item)
    if isinstance(item, (str, bytes, bytearray)):
        return ServerSentEvent(data=item)
    return ServerSentEvent(data=item)


def _ensure_async_iterable(
    iterable: "AsyncIterable[Any] | Iterable[Any]",
) -> AsyncIterable[Any]:
    """Return an async iterable equivalent to ``iterable``.

    Async iterables pass through unchanged. Sync iterables are wrapped
    in a small adapter that yields the same items. We deliberately
    don't add ``await asyncio.sleep(0)`` between yields — sync sources
    are typically tiny finite sequences (test fixtures, smoke tests),
    so blocking the loop briefly is fine.
    """
    if hasattr(iterable, "__aiter__"):
        return iterable  # type: ignore[return-value]

    async def _adapt() -> AsyncIterator[Any]:
        for item in iterable:  # type: ignore[union-attr]
            yield item

    return _adapt()


# ---------------------------------------------------------------------------
# LastEventId convenience
# ---------------------------------------------------------------------------


def last_event_id(headers: Headers) -> str | None:
    """Read the ``Last-Event-ID`` header off a request, if present.

    The browser's ``EventSource`` automatically replays the most
    recently observed ``id:`` value as the ``Last-Event-ID`` header on
    reconnect. Exposing this as a tiny helper means handlers can resume
    server-side cursors without remembering the exact spelling::

        @get("/feed")
        async def feed(self, req: Request) -> EventStream:
            cursor = last_event_id(req.headers) or "0"
            ...

    Returns ``None`` when the header is absent or empty.
    """
    raw = headers.get("last-event-id")
    if raw is None or raw == "":
        return None
    return raw


__all__ = [
    "EventStream",
    "ServerSentEvent",
    "SSEItem",
    "format_sse_event",
    "last_event_id",
]
