"""Public type definitions — Scope, State, Request, Response, ExecutionContext."""

from __future__ import annotations

import enum
import json as jsonlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Annotated,
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    TypeVar,
    Union,
    get_args,
    get_origin,
    overload,
    runtime_checkable,
)
from urllib.parse import parse_qsl, quote

from ._metadata import _UNSET, MetadataKey, MetadataSetKey, StateKey, read_metadata
from ._urls._url import DEFAULT_PORTS, PATH_SAFE, URL, UrlAccess, compose_origin, join_root, parse_authority
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
    type errors. A :class:`~lauren.StateKey` gives a slot a name *and* a type:
    ``state[CurrentUser] = user`` stores under the key's name and
    ``state[CurrentUser]`` reads it back (raising the key's ``on_missing`` —
    :class:`MissingStateError` by default — when absent). Item access, ``in``,
    :meth:`get`, :meth:`set` and :meth:`has` accept a key or a plain string.
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

    @overload
    def __getitem__(self, key: StateKey[T]) -> T: ...
    @overload
    def __getitem__(self, key: str) -> Any: ...
    def __getitem__(self, key: Any) -> Any:
        data: dict[str, Any] = self._data  # type: ignore[attr-defined]
        if isinstance(key, StateKey):
            try:
                return data[key.name]
            except KeyError:
                raise key.missing_error() from None
        try:
            return data[key]
        except KeyError:
            raise MissingStateError(f"required state key {key!r} is missing", detail={"key": key}) from None

    @overload
    def __setitem__(self, key: StateKey[T], value: T) -> None: ...
    @overload
    def __setitem__(self, key: str, value: Any) -> None: ...
    def __setitem__(self, key: StateKey[Any] | str, value: Any) -> None:
        self._data[key.name if isinstance(key, StateKey) else key] = value  # type: ignore[attr-defined]

    def __contains__(self, key: object) -> bool:
        return (key.name if isinstance(key, StateKey) else key) in self._data  # type: ignore[attr-defined]

    @overload
    def set(self, key: StateKey[T], value: T) -> None: ...
    @overload
    def set(self, key: str, value: Any) -> None: ...
    def set(self, key: StateKey[Any] | str, value: Any) -> None:
        self._data[key.name if isinstance(key, StateKey) else key] = value  # type: ignore[attr-defined]

    @overload
    def get(self, key: StateKey[T]) -> T | None: ...
    @overload
    def get(self, key: StateKey[T], default: T) -> T: ...
    @overload
    def get(self, key: str, default: Any = None) -> Any: ...
    def get(self, key: Any, default: Any = None) -> Any:
        return self._data.get(key.name if isinstance(key, StateKey) else key, default)  # type: ignore[attr-defined]

    def has(self, key: StateKey[Any] | str) -> bool:
        return (key.name if isinstance(key, StateKey) else key) in self._data  # type: ignore[attr-defined]

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
            raise MissingStateError(f"required state key {key!r} is missing", detail={"key": key})
        val = self.get_typed(key, expected)
        assert val is not None  # require key exists but value may legitimately be falsy
        return val

    def asdict(self) -> dict[str, Any]:
        return dict(self._data)  # type: ignore[attr-defined]


class AppState(State):
    """Read-only application-level state.

    Writes raise :class:`RuntimeError` after the app has been sealed (once
    startup completed). ``StateKey`` item access works as on :class:`State`.
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

    @overload
    def __setitem__(self, key: StateKey[T], value: T) -> None: ...
    @overload
    def __setitem__(self, key: str, value: Any) -> None: ...
    def __setitem__(self, key: StateKey[Any] | str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise RuntimeError("AppState is sealed and read-only after startup")
        super().__setitem__(key, value)

    @overload
    def set(self, key: StateKey[T], value: T) -> None: ...
    @overload
    def set(self, key: str, value: Any) -> None: ...
    def set(self, key: StateKey[Any] | str, value: Any) -> None:
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


@dataclass(frozen=True, slots=True)
class ContentConfig:
    """Application-wide request-body content policy.

    Pass it to ``LaurenFactory.create(..., content=ContentConfig(...))`` (or
    ``Lauren(content=...)``). ``strict_content_type`` decides what a JSON body
    parameter — ``Json[T]``, an implicit model body, ``Discriminated[...]`` —
    does with the request's ``Content-Type`` before any body byte is read:

    * ``True`` — only ``application/json`` and ``application/*+json`` (charset
      absent or UTF-8) are accepted; anything else, including a body announced
      by ``Content-Length`` > 0 or ``Transfer-Encoding`` without a
      ``Content-Type``, is rejected with 415
      :class:`~lauren.exceptions.UnsupportedMediaTypeError`. This stops
      cross-site "simple" requests (HTML forms, ``fetch`` in ``no-cors``
      mode) from reaching cookie-authenticated JSON routes.
    * ``"warn"`` — the same check, but mismatches are accepted and logged
      once per route and media type (a bounded number per route), so the
      impact can be measured before switching to ``True``.
    * ``False`` — no check.
    * ``None`` (default) — ``"warn"`` when ``sessions=`` is configured,
      otherwise ``False``. Setting any explicit value also silences the
      startup warning session-enabled apps get.

    The same policy applies to ``Form[...]`` bodies. Routes that declare
    ``consumes=[...]`` and ``UploadFile`` parameters (``multipart/form-data``)
    are always checked.

    * ``malformed_body_status`` — the status of a body that cannot be decoded
      (malformed JSON): ``422`` (default; ``detail.reason`` is
      ``"malformed_body"``) or ``400`` (:class:`~lauren.exceptions.BadRequestError`
      with ``line`` / ``col`` when known).
    * ``codecs`` — extra :class:`~lauren.codecs.Codec` instances, e.g.
      ``[MsgpackCodec()]``. The JSON codec is always present; pass a
      ``JsonCodec(backend="stdlib")`` to configure it.
    * ``default_produces`` — the response media types of routes without
      ``produces=``, in preference order (``("application/json",)``).
    * ``strict_accept`` — answer 406 :class:`~lauren.exceptions.NotAcceptableError`
      when ``Accept`` excludes every ``produces`` entry, instead of serving
      the first one.
    """

    strict_content_type: bool | Literal["warn"] | None = None
    malformed_body_status: int = 422
    codecs: Sequence[Any] = ()
    default_produces: Sequence[str] = ("application/json",)
    strict_accept: bool = False

    def __post_init__(self) -> None:
        # Lists are accepted for convenience; retain an immutable snapshot.
        for name in ("codecs", "default_produces"):
            value = getattr(self, name)
            if isinstance(value, list):
                object.__setattr__(self, name, tuple(value))


@dataclass(frozen=True, slots=True)
class DebugConfig:
    """Development error pages, enabled with ``LaurenFactory.create(..., debug=...)``.

    ``debug=True`` is shorthand for ``debug=DebugConfig()``. When an exception
    reaches the framework unhandled (a 500), an allowed client gets a
    read-only report — exception chain, traceback with source context, the
    matched route and its middleware / guard / interceptor chain, the
    extraction plan and a redacted request summary. Clients whose ``Accept``
    prefers ``text/html`` get a self-contained HTML page; others get the
    normal error envelope plus a top-level ``debug`` member.

    * ``include_4xx`` — also attach ``debug.raised_at`` (where the error was
      raised, plus a short traceback) to rendered :class:`~lauren.HTTPError`
      responses of any status.
    * ``allow_remote`` — serve debug output to every client. By default only
      loopback clients (``127.0.0.0/8``, ``::1``) get it; behind a reverse
      proxy on the same host every request looks like loopback, so this is
      **not** a security boundary.
    * ``redact_headers`` / ``redact_query_keys`` — header names and query
      keys whose values are replaced with ``[redacted]``; a name matches when
      it contains one of the entries (case-insensitive).
    * ``source_context`` — source lines shown around each frame's line.
    * ``max_frames`` — innermost frames kept per traceback.
    * ``include_locals`` — show each frame's local variables (``repr``,
      truncated; names matching ``redact_query_keys`` are redacted). Off by
      default because locals routinely hold secrets.

    Never enable debug pages in production: ``create()`` logs a warning
    whenever they are on.
    """

    include_4xx: bool = False
    allow_remote: bool = False
    redact_headers: tuple[str, ...] = (
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-api-key",
    )
    redact_query_keys: tuple[str, ...] = ("token", "password", "secret", "api_key")
    source_context: int = 5
    max_frames: int = 40
    include_locals: bool = False


class Request(UrlAccess):
    """Incoming HTTP request.

    The request owns its ASGI scope and the ``receive`` callable required to
    consume the body. State, route metadata, and app state are attached by the
    runtime before the handler executes.

    The URL model — ``scheme``, ``host``, ``port``, ``root_path``,
    ``base_url``, ``full_url`` and ``client`` — comes from the ASGI scope and
    the ``Host`` header, or from a trusted proxy when the app is built with
    ``proxy=ProxyConfig(...)``. ``url_for`` / ``url_path_for`` /
    ``ws_url_for`` build URLs to the app's named routes.

    Instances are **pooled and reused** by :class:`lauren._arena.RequestArena`:
    ``reset()`` clears the instance's entire ``__dict__`` between requests, so
    an attribute set here (e.g. ``request.tenant_id = ...``) is visible only for
    the lifetime of the current request and must never be relied upon to
    persist. Copy anything you need to keep.
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
        extensions: dict[str, Any] | None = None,
        scheme: str = "http",
        root_path: str = "",
        authority: str | None = None,
        urls: Any = None,
    ) -> None:
        self._method = method.upper()
        self._path = path
        self._raw_query_string = raw_query_string
        self._headers = headers or Headers()
        self._path_params: dict[str, str] = dict(path_params or {})
        # ``{name}`` labels of a host-routed request (``HostParam``); set by
        # the router, shared empty mapping otherwise.
        self._host_params: Mapping[str, str] = _EMPTY_HOST_PARAMS
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
        # URL model: ASGI scheme, public root path, a trusted proxy's
        # ``host[:port]`` (``None``: the Host header) and the app's UrlBuilder.
        self._scheme = scheme
        self._root_path = root_path
        self._authority_value = authority
        self._urls = urls
        # ASGI ``scope["extensions"]``: stored only when the server sent some,
        # so the common case adds no attribute (see the ``extensions`` property).
        if extensions:
            self._extensions = extensions

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
        extensions: dict[str, Any] | None = None,
        scheme: str = "http",
        root_path: str = "",
        authority: str | None = None,
        urls: Any = None,
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

        ``Request`` is deliberately **not** slotted, so *all* state —
        the canonical fields below and anything user code, middleware,
        or an extension attached — lives in ``self.__dict__``. ``reset``
        therefore clears the whole ``__dict__`` before re-populating the
        canonical fields, which guarantees no attribute from the previous
        request survives onto this one. This is a correctness invariant,
        not hygiene: a leaked attribute would be observable on an
        unrelated request served by the same pooled instance.
        """
        # Clear *every* attribute before re-populating — canonical fields
        # and anything user code, middleware, or an extension attached
        # (e.g. ``request.tenant_id``, or the multipart parse cache the
        # UploadFile extractor stashes) alike. Because the class is not
        # slotted this is a single O(n) clear on a tiny dict, and it
        # removes the need to keep a hand-maintained deny-list of
        # "attributes that must not leak" in sync with every contributor
        # that stashes state on the request.
        self.__dict__.clear()

        self._method = method.upper()
        self._path = path
        self._raw_query_string = raw_query_string
        self._headers = headers
        # ``path_params`` is (re-)populated by the router. Assign a
        # *fresh* dict rather than clearing the existing one: the previous
        # request's code may still hold a reference to the very dict
        # ``path_params`` returned, and clearing it in place would mutate
        # what that code sees. (Same reasoning as the fresh ``State()``
        # below.)
        self._path_params = {}
        self._host_params = _EMPTY_HOST_PARAMS
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
        self._scheme = scheme
        self._root_path = root_path
        self._authority_value = authority
        self._urls = urls
        if extensions:
            self._extensions = extensions

    # -- Core properties ---------------------------------------------------

    @property
    def method(self) -> str:
        return self._method

    @property
    def path(self) -> str:
        return self._path

    @property
    def url(self) -> str:
        """The app-relative path and query (``/users/7?x=1``); see ``full_url`` for the absolute URL."""
        qs = self._raw_query_string.decode("latin-1")
        if qs:
            return f"{self._path}?{qs}"
        return self._path

    @property
    def path_with_query(self) -> str:
        """``path?query`` — the same value as ``url``, which lauren 2.0 makes absolute."""
        return self.url

    # -- URL model ----------------------------------------------------------

    @property
    def scheme(self) -> str:
        """``"http"`` or ``"https"`` — from the ASGI scope or a trusted proxy."""
        return self._scheme

    @property
    def root_path(self) -> str:
        """The public path prefix in front of ``path`` (``/backend``): mounts, ``root_path=``, a trusted proxy."""
        return self._root_path

    def _host_port(self) -> tuple[str, int | None]:
        value = self._authority_value
        if value is None:
            value = self._headers.get("host") or self._headers.get(":authority")
        if value:
            parsed = parse_authority(value.strip())
            if parsed is not None:
                return parsed
        host = self._server.host
        if not host:
            return "", None
        return (f"[{host}]" if ":" in host else host), self._server.port

    @property
    def host(self) -> str:
        """The host the client addressed: a trusted proxy's, the ``Host`` header's, or the server's.

        IPv6 literals keep their brackets. An invalid ``Host`` header is
        ignored. Without a trusted proxy the ``Host`` header is client-chosen —
        build links that leave the request (emails, webhooks) with
        :class:`~lauren.urls.UrlBuilder` and ``public_base_url=`` instead.
        """
        return self._host_port()[0]

    @property
    def port(self) -> int:
        """The port the client addressed (the scheme's default when the host names none)."""
        port = self._host_port()[1]
        return port if port is not None else DEFAULT_PORTS.get(self._scheme, 80)

    def _url_parts(self) -> tuple[str, str, int, str, Any]:
        host, port = self._host_port()
        if port is None:
            port = DEFAULT_PORTS.get(self._scheme, 80)
        return self._scheme, host, port, self._root_path, self._urls

    @property
    def full_url(self) -> URL:
        """The absolute request URL: ``scheme://host[:port]{root_path}{path}[?query]``."""
        scheme, host, port, root, _ = self._url_parts()
        url = compose_origin(scheme, host, port) + join_root(root, quote(self._path, safe=PATH_SAFE))
        qs = self._raw_query_string.decode("latin-1")
        return URL(f"{url}?{qs}" if qs else url)

    @property
    def path_params(self) -> dict[str, str]:
        return self._path_params

    @property
    def host_params(self) -> Mapping[str, str]:
        """``{name}`` labels of the matched ``host=`` pattern (lower-cased; empty otherwise)."""
        return self._host_params

    @property
    def query_params(self) -> dict[str, list[str]]:
        if self._query_params is None:
            parsed: dict[str, list[str]] = {}
            for k, v in parse_qsl(self._raw_query_string.decode("latin-1"), keep_blank_values=True):
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

    @property
    def extensions(self) -> Mapping[str, Any]:
        """The ASGI ``scope["extensions"]`` of this request, read-only.

        Servers advertise optional protocol features here (``"tls"``,
        ``"http.response.trailers"``, …); in-process drivers such as
        :class:`lauren.testing.TestClient` use it to correlate a request with
        the test that sent it. Empty when the server sent no extensions.
        """
        # ``reset()`` cleared ``__dict__``, so an absent attribute reliably
        # means "this request carried no extensions".
        ext = self.__dict__.get("_extensions")
        if not ext:
            return _EMPTY_EXTENSIONS
        return MappingProxyType(ext)

    # -- Body consumption --------------------------------------------------

    def _set_body_limit(self, limit: int) -> None:
        """Apply a route's ``max_body_size`` (called by the dispatcher right after routing).

        Raises :class:`~lauren.exceptions.RequestBodyTooLarge` when the body was
        already buffered (by a global middleware) and exceeds ``limit``.
        """
        self._max_body_size = limit
        if self._body is not None and len(self._body) > limit:
            raise RequestBodyTooLarge(f"body exceeds {limit} bytes", detail={"max_body_size": limit})

    def _reject_declared_oversize(self) -> None:
        """Raise ``RequestBodyTooLarge`` when ``Content-Length`` already exceeds the limit.

        Runs before the first ``receive()``; an absent or invalid header leaves
        the decision to the streaming byte count.
        """
        declared = self._headers.get("content-length")
        if declared is None:
            return
        declared = declared.strip()
        if not (declared.isascii() and declared.isdigit()):
            return
        # Compare decimal strings instead of int(declared): Python limits the
        # number of digits it will convert, and an attacker can supply a much
        # longer but syntactically valid Content-Length. Still answer 413 before
        # awaiting receive, without a ValueError turning into a 500.
        digits = declared.lstrip("0") or "0"
        maximum = str(self._max_body_size)
        if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
            raise RequestBodyTooLarge(
                f"body exceeds {self._max_body_size} bytes",
                detail={"max_body_size": self._max_body_size},
            )

    async def body(self) -> bytes:
        if self._body is not None:
            return self._body
        self._reject_declared_oversize()
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
        self._reject_declared_oversize()
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


_EMPTY_EXTENSIONS: Mapping[str, Any] = MappingProxyType({})
_EMPTY_HOST_PARAMS: Mapping[str, str] = MappingProxyType({})


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
        return f"UploadFile(filename={self.filename!r}, content_type={self.content_type!r}, size={self.size})"


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

    The effective ``max_body_size`` (the route's, else the app's) is
    enforced across the stream: a ``Content-Length`` above it is rejected
    before anything is read, and if the cumulative chunk total exceeds the
    limit, iteration raises
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
        if self._total == 0:
            try:
                req._reject_declared_oversize()
            except RequestBodyTooLarge:
                self._consumed = True
                raise
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
        # A negotiated response codec (``produces=``) labels its own bytes.
        media_type = getattr(encoder, "lauren_media_type", None) or "application/json"
        return cls(body, status=status, headers=headers, media_type=media_type)

    @classmethod
    def text(cls, data: str, *, status: int = 200, headers: Headers | None = None) -> "Response":
        return cls(
            data,
            status=status,
            headers=headers,
            media_type="text/plain; charset=utf-8",
        )

    @classmethod
    def html(cls, data: str, *, status: int = 200, headers: Headers | None = None) -> "Response":
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
    def created(cls, data: Any | None = None, *, location: str | None = None) -> "Response":
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
    async def file(
        cls,
        path: str,
        *,
        media_type: str | None = None,
        filename: str | None = None,
        inline: bool = False,
        chunk_size: int = 65536,
        headers: "Headers | None" = None,
    ) -> "Response":
        """Stream a file from the filesystem asynchronously.

        Uses ``anyio.open_file`` for non-blocking reads so the event loop
        is never blocked, even for large files.  MIME type is auto-detected
        from the file extension when ``media_type`` is omitted.

        :param path: Filesystem path to the file (``str`` or ``Path``).
        :param media_type: Content-Type override.  Detected automatically
            from the extension when ``None`` (falls back to
            ``application/octet-stream`` for unknown extensions).
        :param filename: Name sent in the ``Content-Disposition`` header.
            Defaults to the basename of ``path``.
        :param inline: When ``True`` the browser displays the file inline
            (``Content-Disposition: inline``).  When ``False`` (the default)
            the browser opens a Save-As dialog
            (``Content-Disposition: attachment``).
        :param chunk_size: Read buffer size in bytes.  Default is 64 KB.
        :param headers: Extra response headers merged before the
            Content-Type and Content-Disposition headers are applied.
        :raises FileNotFoundError: When ``path`` does not point to an
            existing file.
        :return: A streaming :class:`Response` ready to be returned from
            a handler.

        Example — serve a generated PDF::

            @get("/report")
            async def report(self) -> Response:
                return await Response.file(
                    "/tmp/report.pdf",
                    filename="quarterly-report.pdf",
                )

        Example — serve an image inline::

            @get("/logo")
            async def logo(self) -> Response:
                return await Response.file(
                    "static/logo.png",
                    inline=True,
                )
        """
        import mimetypes
        import pathlib

        import anyio

        file_path = pathlib.Path(path)  # type: ignore[arg-type]
        if not file_path.is_file():
            raise FileNotFoundError(f"Response.file: no such file: {file_path!s}")

        resolved_media_type = media_type
        if resolved_media_type is None:
            guessed, _ = mimetypes.guess_type(str(file_path))
            resolved_media_type = guessed or "application/octet-stream"

        resolved_filename = filename or file_path.name
        disposition_type = "inline" if inline else "attachment"
        content_disposition = f'{disposition_type}; filename="{resolved_filename}"'

        _chunk = chunk_size

        async def _stream() -> AsyncIterator[bytes]:
            async with await anyio.open_file(str(file_path), "rb") as fh:
                while True:
                    chunk = await fh.read(_chunk)
                    if not chunk:
                        break
                    yield chunk

        hdr = MutableHeaders(list((headers or Headers()).raw()))
        hdr.set("content-disposition", content_disposition)

        return cls(
            b"",
            status=200,
            headers=hdr,
            media_type=resolved_media_type,
            stream=_stream(),
        )

    @classmethod
    def xml(
        cls,
        data: str,
        *,
        status: int = 200,
        headers: "Headers | None" = None,
    ) -> "Response":
        """Build an XML response with ``Content-Type: application/xml``.

        :param data: Raw XML content as a string (encoded to UTF-8) or bytes.
        :param status: HTTP status code (default 200).
        :param headers: Optional extra headers.

        Example::

            @get("/feed")
            async def atom_feed(self) -> Response:
                xml = "<feed>...</feed>"
                return Response.xml(xml)
        """
        if isinstance(data, str):
            data = data.encode("utf-8")  # type: ignore[assignment]
        return cls(data, status=status, headers=headers, media_type="application/xml")

    @classmethod
    def sse(
        cls,
        iterable: AsyncIterable[str | dict[str, Any]],
        *,
        status: int = 200,
        encoder: Any = None,
    ) -> "Response":
        async def _wrap() -> AsyncIterator[bytes]:
            from .serialization import get_active_encoder  # noqa: PLC0415

            _enc = encoder or get_active_encoder()
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
                        payload = _enc.encode_compact(payload).decode("utf-8")
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
        new = type(self).__new__(type(self))
        new._status = self._status if status is None else status
        new._headers = MutableHeaders(list(self._headers.raw())) if headers is None else headers
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


def _json_default(obj: Any, fallback: Any = None) -> Any:
    """Fallback JSON encoder covering common rich types.

    Lauren's default response builder invokes this via :func:`json.dumps`
    whenever it encounters a value that is not natively JSON-serializable.
    It knows how to handle Pydantic v2 models, standard-library datetimes,
    :class:`enum.Enum`, :class:`uuid.UUID`, :class:`pathlib.PurePath`,
    :class:`decimal.Decimal`, :class:`set` / :class:`frozenset`, and
    dataclass instances. Secrets (:class:`lauren.config.Secret`, pydantic
    ``SecretStr`` / ``SecretBytes``) encode as ``"**********"``.

    Any other object with a ``__dict__`` goes through the ``json_fallback``
    policy: ``fallback`` (an encoder's pinned policy) or else the policy of the
    app handling the current request; outside an app its public attributes
    are dumped (the historical behaviour).
    """
    import enum
    import dataclasses
    import datetime
    import decimal
    import pathlib
    import uuid

    from ._config._secret import REDACTED, is_secret

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
    # Secrets (lauren.config.Secret, pydantic SecretStr / SecretBytes) never
    # leak; checked after the common rich types so they pay nothing for it.
    if is_secret(obj):
        return REDACTED
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    # msgspec.Struct — detected via __struct_fields__ without importing msgspec.
    # Converts to a plain dict so orjson / stdlib encoders can handle the value.
    if hasattr(obj, "__struct_fields__"):
        return {field: getattr(obj, field) for field in obj.__struct_fields__}
    if hasattr(obj, "__dict__"):
        # Last resort: the instance's public data attributes — subject to the
        # json_fallback policy ("dict" / "warn" / "error").
        from .serialization import _object_fallback

        return _object_fallback(obj, fallback)
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
    """Contextual info passed to guards, interceptors and extractors.

    ``metadata`` is the route's merged ``@set_metadata`` / typed-key mapping,
    compiled once at startup and shared read-only by every request.
    """

    request: Request
    handler_class: type | None = None
    handler_func: Callable[..., Any] | None = None
    route_template: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @overload
    def get_metadata(self, key: MetadataSetKey[T]) -> tuple[T, ...]: ...
    @overload
    def get_metadata(self, key: MetadataKey[T]) -> T: ...
    @overload
    def get_metadata(self, key: str, default: Any = None) -> Any: ...
    def get_metadata(self, key: Any, default: Any = _UNSET) -> Any:
        """Read route metadata: a typed key returns its typed value (or its default); a string reads raw."""
        return read_metadata(self.metadata, key, default)


@runtime_checkable
class GuardProtocol(Protocol):
    async def can_activate(self, context: ExecutionContext) -> bool: ...


class CallHandler:
    """Represents the rest of the interceptor / handler pipeline.

    Passed to every :class:`InterceptorProtocol` as the second argument.
    Call :meth:`handle` to advance to the next interceptor (or, for the
    innermost interceptor, to the actual route handler itself).

    The return value of :meth:`handle` is **always a** :class:`Response`
    object — the raw handler result (dict, Pydantic model, tuple, etc.)
    has already been coerced by the time interceptors see it.  This means
    an interceptor can always safely read ``result.status_code``,
    ``result.body``, ``result.headers``, etc. without type-guarding::

        @interceptor()
        class TimingInterceptor:
            async def intercept(
                self, ctx: ExecutionContext, call_handler: CallHandler
            ) -> Response:
                start = time.perf_counter()
                result = await call_handler.handle()   # always Response
                elapsed = time.perf_counter() - start
                # Augment and return the coerced response directly.
                return result.with_header("x-duration-ms", f"{elapsed * 1000:.1f}")
    """

    def __init__(self, fn: "Callable[[], Awaitable[Any]]") -> None:
        self._fn = fn

    async def handle(self) -> "Response":
        """Invoke the next stage in the pipeline and return a coerced :class:`Response`."""
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

    async def intercept(self, context: ExecutionContext, call_handler: CallHandler) -> "Any": ...


# ---------------------------------------------------------------------------
# Discriminated — pydantic-free discriminated union type
# ---------------------------------------------------------------------------


class _DiscriminatedMeta(type):
    """Metaclass enabling the ``Discriminated[Union[A, B], 'key']`` syntax."""

    def __getitem__(cls, params: tuple) -> Any:  # type: ignore[override]
        if not isinstance(params, tuple) or len(params) != 2:
            raise TypeError("Discriminated requires exactly two arguments: Discriminated[Union[A, B], 'key']")
        union_type, key = params
        if not isinstance(key, str):
            raise TypeError(f"Discriminated key must be a string, got {type(key).__name__}")
        return _build_discriminated_annotation(union_type, key)


def _build_discriminated_annotation(union_type: Any, key: str) -> Any:
    """Construct Annotated[union_type, _DiscriminatorMarker(key, mapping)]."""
    from ._discriminated import _DiscriminatorMarker  # noqa: PLC0415

    variants: list[type] = []
    origin = get_origin(union_type)
    if origin is Union:
        variants = [a for a in get_args(union_type) if a is not type(None)]
    elif isinstance(union_type, type):
        variants = [union_type]
    else:
        try:
            import types as _types  # noqa: PLC0415

            if isinstance(union_type, _types.UnionType):
                variants = list(union_type.__args__)
        except AttributeError:
            pass

    if not variants:
        raise TypeError(
            f"Discriminated[..., '{key}']: could not extract variants from {union_type!r}. "
            "Use Union[A, B] or A | B syntax."
        )

    mapping: dict[str, type] = {}
    for variant in variants:
        tag = _extract_discriminator_tag(variant, key)
        if tag is None:
            raise TypeError(
                f"Discriminated[..., '{key}']: variant {variant.__name__!r} has no "
                f"Literal annotation or default value for field '{key}'. "
                f"Add `{key}: Literal['<value>'] = '<value>'` to {variant.__name__}."
            )
        mapping[str(tag)] = variant

    marker = _DiscriminatorMarker(key=key, mapping=mapping)
    return Annotated[union_type, marker]


def _extract_discriminator_tag(variant: type, key: str) -> str | None:
    """Extract the discriminator tag value from a variant type.

    Tries, in order:
      1. msgspec.Struct tag_field/tag class config
      2. Literal annotation: ``kind: Literal["cat"] = "cat"``
      3. dataclass field default
      4. Class attribute default
    """
    import typing as _typing  # noqa: PLC0415
    import dataclasses as _dc  # noqa: PLC0415

    # msgspec Struct with tag_field= / tag= class config
    struct_config = getattr(variant, "__struct_config__", None)
    if struct_config is not None:
        tag_field = getattr(struct_config, "tag_field", None)
        tag = getattr(struct_config, "tag", None)
        if tag_field == key and tag is not None:
            return str(tag)

    from ._typing import resolve_type_hints  # noqa: PLC0415

    try:
        hints = resolve_type_hints(variant, include_extras=False)
    except Exception:
        hints = {}
    ann = hints.get(key)
    if ann is not None and get_origin(ann) is _typing.Literal:
        args = get_args(ann)
        if args:
            return str(args[0])

    if _dc.is_dataclass(variant):
        for f in _dc.fields(variant):  # type: ignore[arg-type]
            if f.name == key and f.default is not _dc.MISSING:
                return str(f.default)

    val = getattr(variant, key, None)
    if val is not None and not callable(val):
        return str(val)

    return None


class Discriminated(metaclass=_DiscriminatedMeta):
    """Pydantic-free discriminated union type for use in route handler signatures.

    Usage::

        from lauren import Discriminated
        from dataclasses import dataclass
        from typing import Literal

        @dataclass
        class Cat:
            kind: Literal["cat"] = "cat"
            name: str = ""

        @dataclass
        class Dog:
            kind: Literal["dog"] = "dog"
            name: str = ""

        @post("/animals")
        async def create(body: Json[Discriminated[Cat | Dog, "kind"]]) -> dict:
            match body:
                case Cat(name=n): return {"animal": "cat", "name": n}
                case Dog(name=n): return {"animal": "dog", "name": n}

    Works equally well with ``msgspec.Struct``, ``TypedDict``, and
    ``pydantic.BaseModel`` variants.
    """


__all__ = [
    "Scope",
    "Headers",
    "MutableHeaders",
    "State",
    "AppState",
    "StateKey",
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
    "Discriminated",
]
