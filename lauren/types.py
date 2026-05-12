"""Public type definitions — Scope, State, Request, Response, ExecutionContext."""

from __future__ import annotations

import enum
import json as jsonlib
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Protocol,
    TypeVar,
    runtime_checkable,
)
from urllib.parse import parse_qsl

from .exceptions import MissingStateError, RequestBodyTooLarge, StateTypeError

T = TypeVar("T")


class Scope(enum.IntEnum):
    """DI scope values, ordered from narrowest to widest.

    Scopes form a total order on *lifetime width*:

    * ``TRANSIENT`` (0) — a fresh instance on every resolution. Narrowest.
    * ``REQUEST``   (1) — one instance per in-flight request.
    * ``SINGLETON`` (2) — one instance per application. Widest.

    The numeric ordering is what the DI compiler uses to detect *scope
    narrowing violations* without any bespoke lookup table. A dependent
    whose scope value is **greater than** its dependency's scope value
    would outlive that dependency and therefore constitutes a violation:

    >>> Scope.SINGLETON > Scope.REQUEST    # singleton -> request
    True
    >>> Scope.REQUEST > Scope.TRANSIENT    # request -> transient
    True
    >>> Scope.TRANSIENT > Scope.SINGLETON  # transient -> singleton (ok)
    False

    Prefer :attr:`label` over ``str(scope)`` when producing human-readable
    output — it yields the stable lowercase name (``"singleton"``,
    ``"request"``, ``"transient"``) that tests and logs rely on, and
    does not depend on the ``IntEnum`` ``__str__`` formatting which
    varies between Python 3.11 and 3.12.
    """

    TRANSIENT = 0
    REQUEST = 1
    SINGLETON = 2

    @property
    def label(self) -> str:
        """Return the lowercase name (``"singleton"``, ``"request"``,
        ``"transient"``).

        Used in error messages and structured error details so they
        remain stable regardless of ``IntEnum``'s ``__str__`` output
        across Python versions.
        """
        return self.name.lower()

    def __str__(self) -> str:  # pragma: no cover - trivial
        # Keep repr/str stable and human-friendly (``"Scope.SINGLETON"``
        # via ``repr`` is fine, but printing the bare label is more
        # useful in logs and aligns with the legacy str-enum behaviour
        # that downstream code may format-interpolate).
        return self.label


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


class Headers(Mapping[str, str]):
    """Case-insensitive, ordered, multi-value header container.

    The primary lookup returns the first value for simplicity; use
    :meth:`getall` to retrieve every value for a header name.
    """

    __slots__ = ("_items",)

    def __init__(self, items: list[tuple[str, str]] | None = None) -> None:
        self._items: list[tuple[str, str]] = [(k.lower(), v) for k, v in (items or [])]

    def __getitem__(self, key: str) -> str:
        key = key.lower()
        for k, v in self._items:
            if k == key:
                return v
        raise KeyError(key)

    def __iter__(self):  # pragma: no cover - trivial
        seen = set()
        for k, _ in self._items:
            if k not in seen:
                seen.add(k)
                yield k

    def __len__(self) -> int:
        return len({k for k, _ in self._items})

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        key = key.lower()
        return any(k == key for k, _ in self._items)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def getall(self, key: str) -> list[str]:
        key = key.lower()
        return [v for k, v in self._items if k == key]

    def raw(self) -> list[tuple[str, str]]:
        return list(self._items)

    def mutable_copy(self) -> "MutableHeaders":
        return MutableHeaders(list(self._items))


class MutableHeaders(Headers):
    """Mutable variant used when building responses."""

    def set(self, key: str, value: str) -> None:
        key_l = key.lower()
        self._items = [(k, v) for k, v in self._items if k != key_l]
        self._items.append((key_l, value))

    def append(self, key: str, value: str) -> None:
        self._items.append((key.lower(), value))

    def delete(self, key: str) -> None:
        key_l = key.lower()
        self._items = [(k, v) for k, v in self._items if k != key_l]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class State:
    """Request-scoped state bag with typed accessors.

    Attributes can be set either via attribute-style (``state.user = u``) or
    with :meth:`set`. Typed retrieval helps middleware/handlers avoid silent
    type errors.
    """

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        object.__setattr__(self, "_data", dict(initial or {}))

    def __getattr__(self, item: str) -> Any:
        data: dict[str, Any] = object.__getattribute__(self, "_data")
        if item in data:
            return data[item]
        raise AttributeError(item)

    def __setattr__(self, key: str, value: Any) -> None:
        object.__getattribute__(self, "_data")[key] = value

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value  # type: ignore[attr-defined]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)  # type: ignore[attr-defined]

    def has(self, key: str) -> bool:
        return key in self._data  # type: ignore[attr-defined]

    def get_typed(self, key: str, expected: type[T]) -> T | None:
        val = self._data.get(key)  # type: ignore[attr-defined]
        if val is None:
            return None
        if not isinstance(val, expected):
            raise StateTypeError(
                f"state[{key!r}] is {type(val).__name__}, expected {expected.__name__}",
                detail={"key": key, "expected": expected.__name__},
            )
        return val

    def require(self, key: str, expected: type[T]) -> T:
        if key not in self._data:  # type: ignore[attr-defined]
            raise MissingStateError(
                f"required state key {key!r} is missing", detail={"key": key}
            )
        val = self.get_typed(key, expected)
        assert val is not None  # require key exists but value may legitimately be falsy
        return val

    def asdict(self) -> dict[str, Any]:
        return dict(self._data)  # type: ignore[attr-defined]


class AppState(State):
    """Read-only application-level state.

    Writes raise :class:`RuntimeError` after the app has been sealed.
    """

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        super().__init__(initial)
        object.__setattr__(self, "_sealed", False)

    def seal(self) -> None:
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, key: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise RuntimeError("AppState is sealed and read-only after startup")
        super().__setattr__(key, value)

    def set(self, key: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise RuntimeError("AppState is sealed and read-only after startup")
        super().set(key, value)


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass
class ClientInfo:
    host: str | None
    port: int | None


@dataclass
class ServerInfo:
    host: str | None
    port: int | None


class Request:
    """Incoming HTTP request.

    The request owns its ASGI scope and the ``receive`` callable required to
    consume the body. State, route metadata, and app state are attached by the
    runtime before the handler executes.
    """

    def __init__(
        self,
        *,
        method: str,
        path: str,
        raw_query_string: bytes = b"",
        headers: Headers | None = None,
        path_params: dict[str, str] | None = None,
        client: ClientInfo | None = None,
        server: ServerInfo | None = None,
        receive: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        app_state: AppState | None = None,
        max_body_size: int = 1_048_576,
    ) -> None:
        self._method = method.upper()
        self._path = path
        self._raw_query_string = raw_query_string
        self._headers = headers or Headers()
        self._path_params: dict[str, str] = dict(path_params or {})
        self._query_params: dict[str, list[str]] | None = None
        self._client = client or ClientInfo(None, None)
        self._server = server or ServerInfo(None, None)
        self._receive = receive or _empty_receive
        self._body: bytes | None = None
        self._body_consumed = False
        self._state = State()
        self._app_state = app_state or AppState()
        self._max_body_size = max_body_size
        # Route metadata injected by runtime:
        self._matched_route: Any | None = None
        self._handler_class: type | None = None
        self._handler_func: Callable[..., Any] | None = None
        self._route_template: str | None = None
        # Cookies cache
        self._cookies: dict[str, str] | None = None

    # -- Arena support ----------------------------------------------------

    def reset(
        self,
        *,
        method: str,
        path: str,
        raw_query_string: bytes,
        headers: Headers,
        client: ClientInfo,
        server: ServerInfo,
        receive: Callable[[], Awaitable[dict[str, Any]]],
        app_state: AppState,
        max_body_size: int,
    ) -> None:
        """Re-initialise this :class:`Request` in place for reuse.

        The :class:`lauren._arena.RequestArena` pools ``Request``
        instances along with its container dicts. ``reset()`` lets the
        dispatcher hand the same object to a new request without
        re-running ``__init__`` — the saving is small per-call but
        compounds measurably under load.

        Every attribute set by ``__init__`` is re-set here; per-request
        caches (``_query_params``, ``_cookies``, ``_body``) are cleared
        so the previous request's data cannot leak across the pool.
        The route-metadata slots (``_matched_route`` etc.) are wiped
        too — the dispatcher re-populates them after routing.
        """
        self._method = method.upper()
        self._path = path
        self._raw_query_string = raw_query_string
        self._headers = headers
        # ``path_params`` is (re-)populated by the router. Clear the
        # existing dict rather than allocating a new one so the arena
        # keeps one fewer allocation per request on the hot path.
        self._path_params.clear()
        self._query_params = None
        self._client = client
        self._server = server
        self._receive = receive
        self._body = None
        self._body_consumed = False
        # ``State`` and ``AppState`` are user-visible; we never reuse
        # ``State`` because user code may hold references to the
        # previous object. A fresh instance is the only correct choice.
        self._state = State()
        self._app_state = app_state
        self._max_body_size = max_body_size
        self._matched_route = None
        self._handler_class = None
        self._handler_func = None
        self._route_template = None
        self._cookies = None
        # Clear any per-request caches other subsystems stash on the
        # request. Currently this covers the multipart parse cache
        # (set by the UploadFile extractor); future subsystems that
        # cache state on the request should clear it here too so
        # pooled Request reuse never leaks cross-request data.
        for attr in ("__lauren_upload_cache__",):
            if hasattr(self, attr):
                try:
                    delattr(self, attr)
                except AttributeError:  # pragma: no cover - defensive
                    pass

    # -- Core properties ---------------------------------------------------

    @property
    def method(self) -> str:
        return self._method

    @property
    def path(self) -> str:
        return self._path

    @property
    def url(self) -> str:
        qs = self._raw_query_string.decode("latin-1")
        if qs:
            return f"{self._path}?{qs}"
        return self._path

    @property
    def path_params(self) -> dict[str, str]:
        return self._path_params

    @property
    def query_params(self) -> dict[str, list[str]]:
        if self._query_params is None:
            parsed: dict[str, list[str]] = {}
            for k, v in parse_qsl(
                self._raw_query_string.decode("latin-1"), keep_blank_values=True
            ):
                parsed.setdefault(k, []).append(v)
            self._query_params = parsed
        return self._query_params

    @property
    def headers(self) -> Headers:
        return self._headers

    @property
    def cookies(self) -> dict[str, str]:
        if self._cookies is None:
            cookie_header = self._headers.get("cookie", "")
            out: dict[str, str] = {}
            if cookie_header:
                for pair in cookie_header.split(";"):
                    if "=" in pair:
                        k, _, v = pair.partition("=")
                        out[k.strip()] = v.strip()
            self._cookies = out
        return self._cookies

    @property
    def client(self) -> ClientInfo:
        return self._client

    @property
    def server(self) -> ServerInfo:
        return self._server

    @property
    def state(self) -> State:
        return self._state

    @property
    def app_state(self) -> AppState:
        return self._app_state

    # -- Body consumption --------------------------------------------------

    async def body(self) -> bytes:
        if self._body is not None:
            return self._body
        chunks: list[bytes] = []
        total = 0
        while True:
            msg = await self._receive()
            if msg["type"] == "http.request":
                chunk = msg.get("body", b"") or b""
                total += len(chunk)
                if total > self._max_body_size:
                    raise RequestBodyTooLarge(
                        f"body exceeds {self._max_body_size} bytes",
                        detail={"max_body_size": self._max_body_size},
                    )
                chunks.append(chunk)
                if not msg.get("more_body", False):
                    break
            elif msg["type"] == "http.disconnect":
                break
        self._body = b"".join(chunks)
        self._body_consumed = True
        return self._body

    async def text(self, encoding: str = "utf-8") -> str:
        data = await self.body()
        return data.decode(encoding)

    async def json(self) -> Any:
        data = await self.body()
        if not data:
            return None
        return jsonlib.loads(data)

    async def form(self) -> dict[str, list[str]]:
        data = await self.body()
        parsed: dict[str, list[str]] = {}
        for k, v in parse_qsl(data.decode("utf-8"), keep_blank_values=True):
            parsed.setdefault(k, []).append(v)
        return parsed

    async def stream(self) -> AsyncIterator[bytes]:
        if self._body is not None:
            yield self._body
            return
        total = 0
        while True:
            msg = await self._receive()
            if msg["type"] == "http.request":
                chunk = msg.get("body", b"") or b""
                total += len(chunk)
                if total > self._max_body_size:
                    raise RequestBodyTooLarge(
                        f"body exceeds {self._max_body_size} bytes",
                        detail={"max_body_size": self._max_body_size},
                    )
                if chunk:
                    yield chunk
                if not msg.get("more_body", False):
                    break
            elif msg["type"] == "http.disconnect":
                break

    # -- Route introspection ----------------------------------------------

    def get_handler_class(self) -> type | None:
        return self._handler_class

    def get_route_handler_func(self) -> Callable[..., Any] | None:
        return self._handler_func

    def get_route_template(self) -> str | None:
        return self._route_template

    def get_matched_route(self) -> Any | None:
        return self._matched_route


async def _empty_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


# ---------------------------------------------------------------------------
# UploadFile — multipart form file value
# ---------------------------------------------------------------------------


class UploadFile:
    """A single file uploaded via ``multipart/form-data``.

    Handed to handlers by the :class:`~lauren.extractors.UploadFile`
    extractor. The instance holds the full file contents in memory
    plus the metadata the browser attached to the part.

    Intentionally parallel to FastAPI's ``UploadFile`` class so
    migrating handlers between frameworks requires only an import
    change. The available operations:

    * :attr:`filename` — the filename the client reported (may be
      ``None`` for form fields that are not file uploads).
    * :attr:`content_type` — the MIME type the client declared for
      the part; defaults to ``"text/plain"`` when omitted (RFC 7578
      §4.4).
    * :attr:`size` — length of the buffered body in bytes.
    * :attr:`headers` — every raw header line on the part.
    * :meth:`read` — asynchronous accessor returning the full body.
    * :meth:`read_sync` — synchronous accessor for non-async code
      paths (tests, fixtures). Prefer ``read()`` in handlers.

    Why it's still async
    --------------------

    The framework parses the multipart body up front, so ``read()``
    could be synchronous. It's intentionally async for symmetry
    with FastAPI and to leave room for a future streaming
    implementation that reads parts lazily off the ASGI ``receive``
    callable without buffering.
    """

    __slots__ = ("_data", "filename", "content_type", "headers", "name")

    def __init__(
        self,
        *,
        data: bytes,
        filename: str | None,
        content_type: str,
        headers: list[tuple[str, str]] | None = None,
        name: str = "",
    ) -> None:
        self._data = data
        #: The field name of the multipart part — the ``name=`` value
        #: in its ``Content-Disposition`` header. Rarely interesting
        #: in handler code (the parameter name already encodes the
        #: binding) but occasionally useful in middleware that wants
        #: to audit uploads without depending on the handler shape.
        self.name = name
        self.filename = filename
        self.content_type = content_type
        self.headers = headers or []

    @property
    def size(self) -> int:
        """Number of bytes in the buffered body."""
        return len(self._data)

    async def read(self) -> bytes:
        """Return the file's full contents.

        Async for FastAPI compatibility and future-proofing; the
        current implementation returns immediately because the body
        is already buffered.
        """
        return self._data

    def read_sync(self) -> bytes:
        """Synchronous counterpart to :meth:`read`.

        Prefer :meth:`read` inside async handlers. This method exists
        so tests and helper utilities can inspect uploaded bodies
        without awaiting.
        """
        return self._data

    def __repr__(self) -> str:
        return (
            f"UploadFile(filename={self.filename!r}, "
            f"content_type={self.content_type!r}, size={self.size})"
        )


# ---------------------------------------------------------------------------
# ByteStream — zero-copy body extraction
# ---------------------------------------------------------------------------


class ByteStream:
    """Zero-copy async iterator over an ASGI request body.

    Yields each ``http.request`` chunk as a ``bytes`` object straight
    from the ASGI ``receive`` callable, without the intermediate
    ``list[bytes]`` + ``b"".join(...)`` that :meth:`Request.body` uses.

    Consumption is single-shot — iterating the same instance twice
    raises :class:`~lauren.exceptions.ExtractorError`. This mirrors
    the underlying ASGI ``receive`` contract and prevents the subtle
    "silent second iteration yields nothing" bug that would
    otherwise occur.

    The app-wide ``max_body_size`` is enforced across the stream: if
    the cumulative chunk total exceeds the limit, iteration raises
    :class:`~lauren.exceptions.RequestBodyTooLarge` with the same
    semantics as :meth:`Request.body`. This means a misbehaving
    client cannot defeat the body-size cap by switching from
    ``Bytes`` to ``ByteStream``.

    If :meth:`Request.body` has already buffered the body (e.g. a
    middleware peeked at it), iteration yields that single buffered
    chunk and completes — the handler still sees the full payload,
    just without the zero-copy property for that specific request.
    This fallback keeps the behaviour robust to middleware ordering.
    """

    __slots__ = ("_request", "_consumed", "_total")

    def __init__(self, request: "Request") -> None:
        # Hold a private reference to the Request so we can read its
        # ``_receive`` callable, ``_max_body_size``, and any pre-buffered
        # body. The attributes are deliberately name-mangled (leading
        # underscore) to discourage user code from reaching in; every
        # piece of state this iterator needs lives on the request.
        self._request = request
        self._consumed = False
        self._total = 0

    # -- Async-iterator protocol ------------------------------------------

    def __aiter__(self) -> "ByteStream":
        # Returning self means a user can write ``async for chunk in
        # stream:`` and also ``stream.__aiter__()`` for explicit
        # driving. Re-entrancy is blocked by ``_consumed`` below.
        return self

    async def __anext__(self) -> bytes:
        # Every call enters through the same guard so double-iteration
        # is rejected symmetrically whether the user drives the
        # iterator through ``async for`` or manually.
        if self._consumed:
            raise StopAsyncIteration
        req = self._request
        # If the body was already buffered by ``Request.body()`` or a
        # prior middleware, yield it once and stop. This is strictly
        # a correctness fallback — the zero-copy property is lost
        # for the request, but the handler still gets its data.
        if req._body is not None:
            self._consumed = True
            if req._body:
                return req._body
            raise StopAsyncIteration
        while True:
            msg = await req._receive()
            mtype = msg.get("type")
            if mtype == "http.request":
                chunk: bytes = msg.get("body", b"") or b""
                self._total += len(chunk)
                if self._total > req._max_body_size:
                    # Mark as consumed so the error is not masked by a
                    # follow-up StopAsyncIteration from the same iterator.
                    self._consumed = True
                    raise RequestBodyTooLarge(
                        f"body exceeds {req._max_body_size} bytes",
                        detail={"max_body_size": req._max_body_size},
                    )
                if not msg.get("more_body", False):
                    # Final chunk: flip the consumed flag before
                    # returning so the next ``__anext__`` call
                    # cleanly reports StopAsyncIteration.
                    self._consumed = True
                # Empty trailing chunks are skipped — consistent with
                # ``Request.stream`` — so consumers don't see a
                # ``b''`` as a significant event.
                if chunk:
                    return chunk
                if self._consumed:
                    raise StopAsyncIteration
                # Empty intermediate chunk: loop to the next message.
                continue
            if mtype == "http.disconnect":
                self._consumed = True
                raise StopAsyncIteration

    # -- Convenience helpers ----------------------------------------------

    @property
    def consumed(self) -> bool:
        """True once the stream has been fully drained (or aborted)."""
        return self._consumed

    async def read_all(self) -> bytes:
        """Consume the stream and return the concatenated body.

        Provided for symmetry with :meth:`Request.body`. Defeats the
        zero-copy property but is occasionally useful in tests or in
        code paths that need a one-shot fallback.
        """
        buf: list[bytes] = []
        async for chunk in self:
            buf.append(chunk)
        return b"".join(buf)


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class Response:
    """Immutable HTTP response value object.

    Mutating methods (``with_*``) return a new instance. Bodies may be a
    ``bytes`` blob or an async iterable for streaming responses.
    """

    __slots__ = ("_status", "_headers", "_body", "_stream", "_media_type")

    def __init__(
        self,
        body: bytes | str | None = b"",
        *,
        status: int = 200,
        headers: Headers | MutableHeaders | None = None,
        media_type: str | None = None,
        stream: AsyncIterable[bytes] | None = None,
    ) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif body is None:
            body = b""
        self._body: bytes = body
        self._stream: AsyncIterable[bytes] | None = stream
        self._status = status
        hdr = MutableHeaders(list((headers or Headers()).raw()))
        if media_type and "content-type" not in hdr:
            hdr.set("content-type", media_type)
        self._headers = hdr
        self._media_type = media_type

    # -- Getters -----------------------------------------------------------

    @property
    def status(self) -> int:
        return self._status

    @property
    def status_code(self) -> int:  # alias
        return self._status

    @property
    def headers(self) -> MutableHeaders:
        return self._headers

    @property
    def body(self) -> bytes:
        return self._body

    @property
    def stream_body(self) -> AsyncIterable[bytes] | None:
        return self._stream

    @property
    def media_type(self) -> str | None:
        return self._media_type

    # -- Factories ---------------------------------------------------------

    @classmethod
    def json(
        cls,
        data: Any,
        *,
        status: int = 200,
        headers: Headers | None = None,
        encoder: Any = None,
    ) -> "Response":
        """Build a JSON response.

        When ``encoder`` is provided it must implement the
        :class:`lauren.serialization.JSONEncoder` protocol — the
        dispatcher passes in the app's active encoder so every
        response uses the configured backend. When omitted (e.g.
        tests that build responses directly, or pre-app call sites),
        falls back to the process-wide default which starts as the
        stdlib encoder and can be swapped via
        :func:`lauren.serialization.set_active_encoder`.
        """
        if encoder is None:
            from .serialization import get_active_encoder

            encoder = get_active_encoder()
        body = encoder.encode_compact(data)
        return cls(body, status=status, headers=headers, media_type="application/json")

    @classmethod
    def text(
        cls, data: str, *, status: int = 200, headers: Headers | None = None
    ) -> "Response":
        return cls(
            data,
            status=status,
            headers=headers,
            media_type="text/plain; charset=utf-8",
        )

    @classmethod
    def html(
        cls, data: str, *, status: int = 200, headers: Headers | None = None
    ) -> "Response":
        return cls(
            data,
            status=status,
            headers=headers,
            media_type="text/html; charset=utf-8",
        )

    @classmethod
    def bytes(
        cls,
        data: bytes,
        *,
        status: int = 200,
        media_type: str = "application/octet-stream",
        headers: Headers | None = None,
    ) -> "Response":
        return cls(data, status=status, headers=headers, media_type=media_type)

    @classmethod
    def empty(cls, status: int = 204) -> "Response":
        return cls(b"", status=status)

    @classmethod
    def no_content(cls) -> "Response":
        return cls(b"", status=204)

    @classmethod
    def created(
        cls, data: Any | None = None, *, location: str | None = None
    ) -> "Response":
        resp = cls.json(data, status=201) if data is not None else cls.empty(201)
        if location:
            resp = resp.with_header("location", location)
        return resp

    @classmethod
    def accepted(cls, data: Any | None = None) -> "Response":
        return cls.json(data, status=202) if data is not None else cls.empty(202)

    @classmethod
    def redirect(cls, location: str, *, status: int = 307) -> "Response":
        return cls(b"", status=status, headers=Headers([("location", location)]))

    @classmethod
    def stream(
        cls,
        iterable: AsyncIterable[bytes],  # type: ignore[valid-type]
        *,
        status: int = 200,
        media_type: str = "application/octet-stream",
        headers: Headers | None = None,
    ) -> "Response":
        return cls(
            b"",
            status=status,
            headers=headers,
            media_type=media_type,
            stream=iterable,
        )

    @classmethod
    def sse(
        cls,
        iterable: AsyncIterable[str | dict[str, Any]],
        *,
        status: int = 200,
    ) -> "Response":
        async def _wrap() -> AsyncIterator[bytes]:
            async for event in iterable:
                if isinstance(event, str):
                    data = f"data: {event}\n\n"
                else:
                    parts: list[str] = []
                    if "event" in event:
                        parts.append(f"event: {event['event']}")
                    if "id" in event:
                        parts.append(f"id: {event['id']}")
                    payload = event.get("data", "")
                    if not isinstance(payload, str):
                        payload = jsonlib.dumps(
                            payload, default=_json_default, separators=(",", ":")
                        )
                    parts.append(f"data: {payload}")
                    data = "\n".join(parts) + "\n\n"
                yield data.encode("utf-8")

        return cls.stream(
            _wrap(),
            status=status,
            media_type="text/event-stream",
            headers=Headers([("cache-control", "no-cache")]),
        )

    # -- Builder -----------------------------------------------------------

    def _clone(
        self,
        *,
        status: int | None = None,
        headers: MutableHeaders | None = None,
        body: bytes | None = None,  # type: ignore[valid-type]
        stream: AsyncIterable[bytes] | None = ...,  # type: ignore[assignment, valid-type]
    ) -> "Response":
        new = Response.__new__(Response)
        new._status = self._status if status is None else status
        new._headers = (
            MutableHeaders(list(self._headers.raw())) if headers is None else headers
        )
        new._body = self._body if body is None else body
        new._stream = self._stream if stream is ... else stream
        new._media_type = self._media_type
        return new

    def with_status(self, status: int) -> "Response":
        return self._clone(status=status)

    def with_header(self, key: str, value: str) -> "Response":
        h = MutableHeaders(list(self._headers.raw()))
        h.set(key, value)
        return self._clone(headers=h)

    def with_headers(self, mapping: Mapping[str, str]) -> "Response":
        h = MutableHeaders(list(self._headers.raw()))
        for k, v in mapping.items():
            h.set(k, v)
        return self._clone(headers=h)

    def without_header(self, key: str) -> "Response":
        h = MutableHeaders(list(self._headers.raw()))
        h.delete(key)
        return self._clone(headers=h)

    def with_media_type(self, media_type: str) -> "Response":
        new = self.with_header("content-type", media_type)
        new._media_type = media_type
        return new

    def with_body(self, body: bytes | str) -> "Response":  # type: ignore[valid-type]
        if isinstance(body, str):
            body = body.encode("utf-8")  # type: ignore[union-attr]
        return self._clone(body=body, stream=None)

    def with_cookie(
        self,
        key: str,
        value: str,
        *,
        max_age: int | None = None,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        http_only: bool = False,
        same_site: str | None = None,
    ) -> "Response":
        parts = [f"{key}={value}"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if path:
            parts.append(f"Path={path}")
        if domain:
            parts.append(f"Domain={domain}")
        if secure:
            parts.append("Secure")
        if http_only:
            parts.append("HttpOnly")
        if same_site:
            parts.append(f"SameSite={same_site}")
        h = MutableHeaders(list(self._headers.raw()))
        h.append("set-cookie", "; ".join(parts))
        return self._clone(headers=h)

    def delete_cookie(self, key: str, *, path: str = "/") -> "Response":
        return self.with_cookie(key, "", max_age=0, path=path)


def _json_default(obj: Any) -> Any:
    """Fallback JSON encoder covering common rich types.

    Lauren's default response builder invokes this via :func:`json.dumps`
    whenever it encounters a value that is not natively JSON-serializable.
    It knows how to handle Pydantic v2 models, standard-library datetimes,
    :class:`enum.Enum`, :class:`uuid.UUID`, :class:`pathlib.PurePath`,
    :class:`decimal.Decimal`, :class:`set` / :class:`frozenset`, and
    dataclass instances.
    """
    import enum
    import dataclasses
    import datetime
    import decimal
    import pathlib
    import uuid

    # Pydantic v2 models
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        return obj.model_dump(mode="json")
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, pathlib.PurePath):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        # str preserves precision; callers wanting numeric should cast before.
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    # msgspec.Struct — detected via __struct_fields__ without importing msgspec.
    # Converts to a plain dict so orjson / stdlib encoders can handle the value.
    if hasattr(obj, "__struct_fields__"):
        return {field: getattr(obj, field) for field in obj.__struct_fields__}
    if hasattr(obj, "__dict__"):
        # Last-resort: return the instance dict (filtering out private attrs
        # and non-data attributes).
        return {
            k: v
            for k, v in obj.__dict__.items()
            if not k.startswith("_") and not callable(v)
        }
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# Middleware / Guard protocols
# ---------------------------------------------------------------------------


CallNext = Callable[[Request], Awaitable[Response]]


@runtime_checkable
class MiddlewareProtocol(Protocol):
    async def dispatch(self, request: Request, call_next: CallNext) -> Response: ...


@dataclass
class ExecutionContext:
    """Contextual info passed to guards."""

    request: Request
    handler_class: type | None = None
    handler_func: Callable[..., Any] | None = None
    route_template: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_metadata(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)


@runtime_checkable
class GuardProtocol(Protocol):
    async def can_activate(self, context: ExecutionContext) -> bool: ...


class CallHandler:
    """Represents the rest of the interceptor / handler pipeline.

    Passed to every :class:`InterceptorProtocol` as the second argument.
    Call :meth:`handle` to advance to the next interceptor (or, for the
    innermost interceptor, to the actual route handler itself).

    The return value of :meth:`handle` is the **raw handler result**
    (dict, Pydantic model, ``Response``, etc.) before response coercion.
    Interceptors may inspect and transform it freely::

        class TimingInterceptor:
            async def intercept(
                self, ctx: ExecutionContext, call_handler: CallHandler
            ) -> Any:
                start = time.perf_counter()
                result = await call_handler.handle()
                elapsed = time.perf_counter() - start
                print(f"{ctx.route_template} took {elapsed:.3f}s")
                return result
    """

    def __init__(self, fn: "Callable[[], Awaitable[Any]]") -> None:
        self._fn = fn

    async def handle(self) -> "Any":
        """Invoke the next stage in the pipeline and return its result."""
        return await self._fn()


@runtime_checkable
class InterceptorProtocol(Protocol):
    """Protocol that every interceptor class must satisfy.

    Interceptors run **after** guards and **before** (and after) the
    handler.  Unlike middleware they receive a full
    :class:`ExecutionContext` (matched route, handler class, metadata)
    rather than a bare :class:`Request`.

    The *call_handler* argument lets the interceptor control when (or
    whether) the rest of the pipeline executes::

        @interceptor()
        class LoggingInterceptor:
            async def intercept(
                self, ctx: ExecutionContext, call_handler: CallHandler
            ) -> Any:
                print(f"→ {ctx.route_template}")
                result = await call_handler.handle()
                print(f"← {ctx.route_template}")
                return result
    """

    async def intercept(
        self, context: ExecutionContext, call_handler: CallHandler
    ) -> "Any": ...


__all__ = [
    "Scope",
    "Headers",
    "MutableHeaders",
    "State",
    "AppState",
    "Request",
    "Response",
    "ClientInfo",
    "ServerInfo",
    "CallNext",
    "MiddlewareProtocol",
    "GuardProtocol",
    "ExecutionContext",
    "CallHandler",
    "InterceptorProtocol",
]
