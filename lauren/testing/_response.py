"""Response records returned by the in-process test clients.

:class:`TestResponse` keeps the original 1.x surface (``status_code``,
``headers`` as a list of ``(name, value)`` tuples, ``body``, ``text``,
``json()``, ``header()``, ``headers_all()``) and adds the ``httpx`` names
tests reach for (``content``, case-insensitive ``headers.get``, ``cookies``,
``is_success``, ``raise_for_status()``, ``url``, ``history``, …).
:class:`StreamingTestResponse` is the incremental variant returned by
``client.stream(...)``.
"""

from __future__ import annotations

import codecs
import http
import json as jsonlib
from datetime import timedelta
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterable, Iterator, SupportsIndex, overload

if TYPE_CHECKING:
    from ..sse import ServerSentEvent

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class HTTPStatusAssertionError(AssertionError):
    """Raised by :meth:`TestResponse.raise_for_status` for 4xx / 5xx and aborted responses.

    An :class:`AssertionError` (not a :class:`~lauren.exceptions.LaurenError`):
    it reports a failed expectation in a test, not an application error. The
    offending response is available as :attr:`response`.
    """

    def __init__(self, message: str, *, response: "TestResponse") -> None:
        super().__init__(message)
        self.response = response


class ResponseHeaders(list):  # type: ignore[type-arg]
    """Response headers: a ``list`` of ``(name, value)`` tuples with mapping accessors.

    It *is* a list — iteration (``for k, v in r.headers``), ``int`` indexing,
    ``len()`` and equality with a list of tuples behave exactly as before —
    and adds case-insensitive lookups: ``get``, ``getall`` (alias
    ``get_list``), ``headers["content-type"]``, ``keys()``, ``items()`` and
    ``"content-type" in headers``. Like ``httpx``, ``get`` and ``[]`` join
    repeated headers with ``", "``; use ``getall`` for ``set-cookie``.
    """

    __slots__ = ()

    def getall(self, name: str) -> list[str]:
        """Every value of header ``name`` (case-insensitive), in order."""
        wanted = name.lower()
        return [v for k, v in self if k.lower() == wanted]

    get_list = getall

    def get(self, name: str, default: str | None = None) -> str | None:
        """The value of header ``name`` (repeated values joined with ``", "``), or ``default``."""
        values = self.getall(name)
        return ", ".join(values) if values else default

    def keys(self) -> list[str]:
        """Distinct header names, lower-cased, in first-seen order."""
        seen: dict[str, None] = {}
        for k, _ in self:
            seen.setdefault(k.lower(), None)
        return list(seen)

    def values(self) -> list[str]:
        """One (joined) value per distinct header name, matching :meth:`keys`."""
        return [", ".join(self.getall(k)) for k in self.keys()]

    def items(self) -> list[tuple[str, str]]:
        """``(name, joined value)`` per distinct header name; ``list(headers)`` keeps duplicates."""
        return [(k, ", ".join(self.getall(k))) for k in self.keys()]

    def multi_items(self) -> list[tuple[str, str]]:
        """Every ``(name, value)`` pair, duplicates included (``httpx`` spelling of ``list(h)``)."""
        return list(self)

    @overload
    def __getitem__(self, key: SupportsIndex) -> tuple[str, str]: ...

    @overload
    def __getitem__(self, key: slice) -> list[tuple[str, str]]: ...

    @overload
    def __getitem__(self, key: str) -> str: ...

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            value = self.get(key)
            if value is None:
                raise KeyError(key)
            return value
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            wanted = key.lower()
            return any(k.lower() == wanted for k, _ in self)
        return super().__contains__(key)


class SentRequest:
    """The request a :class:`TestResponse` answers (``response.request``)."""

    __slots__ = ("method", "url", "headers", "content")

    def __init__(
        self, method: str, url: str, headers: Iterable[tuple[str, str]], content: bytes | None
    ) -> None:
        self.method = method
        self.url = url
        self.headers = ResponseHeaders(headers)
        #: The body bytes, or ``None`` when the body was streamed.
        self.content = content

    def __repr__(self) -> str:
        return f"<SentRequest [{self.method} {self.url}]>"


def _charset(headers: ResponseHeaders) -> str:
    content_type = headers.get("content-type") or ""
    for param in content_type.split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            candidate = value.strip().strip('"')
            try:
                codecs.lookup(candidate)
            except LookupError:
                break
            return candidate
    return "utf-8"


def _parse_cookie_pairs(set_cookie_values: Iterable[str]) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for raw in set_cookie_values:
        pair = raw.split(";", 1)[0]
        if "=" in pair:
            name, _, value = pair.partition("=")
            if name.strip():
                cookies[name.strip()] = value.strip()
    return cookies


class TestResponse:
    """A buffered response from :class:`~lauren.testing.TestClient`.

    ``status_code`` is ``None`` only for an aborted request that never
    received ``http.response.start`` (see ``disconnect_after=``). An app
    that returns without starting a response is reported as ``500``, as an
    ASGI server would.
    """

    __test__ = False  # not a pytest test class
    # Declared slots (plus ``__dict__`` so tests may still attach attributes).
    __slots__ = (
        "status_code",
        "headers",
        "_body",
        "aborted",
        "url",
        "request",
        "elapsed",
        "history",
        "__dict__",
    )

    def __init__(
        self,
        status_code: int | None,
        headers: Iterable[tuple[str, str]],
        body: bytes,
        *,
        aborted: bool = False,
        url: str = "",
        request: SentRequest | None = None,
        elapsed: timedelta | None = None,
        history: list["TestResponse"] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers if isinstance(headers, ResponseHeaders) else ResponseHeaders(headers)
        self._body = body
        #: ``True`` when the client simulated a disconnect before the response completed.
        self.aborted = aborted
        #: The URL that produced this response (the last hop when redirects were followed).
        self.url = url
        self.request = request
        self.elapsed = elapsed if elapsed is not None else timedelta(0)
        #: Earlier responses of a followed redirect chain, oldest first.
        self.history: list[TestResponse] = list(history or [])

    # -- body ----------------------------------------------------------------

    @property
    def body(self) -> bytes:
        """The raw response body."""
        return self._body

    @body.setter
    def body(self, value: bytes) -> None:
        self._body = value

    @property
    def content(self) -> bytes:
        """The raw response body (``httpx`` name; same as :attr:`body`)."""
        return self.body

    @property
    def encoding(self) -> str:
        """The ``charset`` of the ``content-type`` header, ``utf-8`` when absent."""
        return _charset(self.headers)

    @property
    def text(self) -> str:
        """The body decoded with :attr:`encoding`."""
        return self.body.decode(self.encoding)

    def json(self, **kwargs: Any) -> Any:
        """Parse the body as JSON (``kwargs`` go to :func:`json.loads`)."""
        return jsonlib.loads(self.body.decode("utf-8"), **kwargs)

    # -- headers & cookies ----------------------------------------------------

    def header(self, name: str) -> str | None:
        """The first value of header ``name`` (case-insensitive), or ``None``."""
        values = self.headers.getall(name)
        return values[0] if values else None

    def headers_all(self, name: str) -> list[str]:
        """Every value of header ``name`` (case-insensitive)."""
        return self.headers.getall(name)

    @property
    def cookies(self) -> dict[str, str]:
        """``{name: value}`` of the cookies *this* response set (parsed ``Set-Cookie``)."""
        return _parse_cookie_pairs(self.headers.getall("set-cookie"))

    # -- status helpers --------------------------------------------------------

    @property
    def reason_phrase(self) -> str:
        if self.status_code is None:
            return ""
        try:
            return http.HTTPStatus(self.status_code).phrase
        except ValueError:
            return ""

    @property
    def http_version(self) -> str:
        return "HTTP/1.1"

    @property
    def is_informational(self) -> bool:
        return self.status_code is not None and 100 <= self.status_code < 200

    @property
    def is_success(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def is_redirect(self) -> bool:
        return self.status_code in _REDIRECT_STATUSES and "location" in self.headers

    @property
    def is_client_error(self) -> bool:
        return self.status_code is not None and 400 <= self.status_code < 500

    @property
    def is_server_error(self) -> bool:
        return self.status_code is not None and 500 <= self.status_code < 600

    @property
    def is_error(self) -> bool:
        return self.is_client_error or self.is_server_error

    def raise_for_status(self) -> "TestResponse":
        """Return ``self`` for a non-error response; raise :class:`HTTPStatusAssertionError` otherwise."""
        where = f" for {self.request.method} {self.request.url}" if self.request is not None else ""
        if self.aborted:
            raise HTTPStatusAssertionError(f"request aborted by client{where}", response=self)
        if self.is_error:
            raise HTTPStatusAssertionError(
                f"{'Client' if self.is_client_error else 'Server'} error "
                f"'{self.status_code} {self.reason_phrase}'{where}",
                response=self,
            )
        return self

    # -- dunder -----------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TestResponse):
            return NotImplemented
        return (self.status_code, list(self.headers), self.body, self.aborted) == (
            other.status_code,
            list(other.headers),
            other.body,
            other.aborted,
        )

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"<{type(self).__name__} [{self.status_code}{' aborted' if self.aborted else ''}]>"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def _split_lines(pending: str, *, final: bool) -> tuple[list[str], str]:
    """Split on CR, LF or CRLF only (not every ``str.splitlines`` separator).

    Returns ``(complete_lines, remainder)``; a trailing lone CR is held back
    until the next chunk shows whether an LF follows, unless ``final``.
    """
    lines: list[str] = []
    start = i = 0
    n = len(pending)
    while i < n:
        ch = pending[i]
        if ch == "\n":
            lines.append(pending[start:i])
            i += 1
            start = i
        elif ch == "\r":
            if i + 1 < n:
                lines.append(pending[start:i])
                i += 2 if pending[i + 1] == "\n" else 1
                start = i
            elif final:
                lines.append(pending[start:i])
                i += 1
                start = i
            else:
                break
        else:
            i += 1
    return lines, pending[start:]


def _iter_lines_from(chunks: Iterable[str]) -> Iterator[str]:
    pending = ""
    for text in chunks:
        lines, pending = _split_lines(pending + text, final=False)
        yield from lines
    lines, pending = _split_lines(pending, final=True)
    yield from lines
    if pending:
        yield pending


class _SSEParser:
    """Incremental Server-Sent Events parser (HTML living standard, §9.2.6)."""

    __slots__ = ("_fields", "_data", "_comments")

    def __init__(self) -> None:
        self._fields: dict[str, str] = {}
        self._data: list[str] | None = None
        self._comments: list[str] = []

    def feed_line(self, line: str) -> "ServerSentEvent | None":
        from ..sse import ServerSentEvent

        if line == "":
            if self._data is None and not self._fields and not self._comments:
                return None
            retry_raw = self._fields.get("retry")
            event = ServerSentEvent(
                data="\n".join(self._data) if self._data is not None else None,
                event=self._fields.get("event"),
                id=self._fields.get("id"),
                retry=int(retry_raw) if retry_raw is not None and retry_raw.isdigit() else None,
                comment="\n".join(self._comments) if self._comments else None,
            )
            self._fields, self._data, self._comments = {}, None, []
            return event
        if line.startswith(":"):
            self._comments.append(line[1:].removeprefix(" "))
            return None
        field, sep, value = line.partition(":")
        if sep:
            value = value.removeprefix(" ")
        if field == "data":
            if self._data is None:
                self._data = []
            self._data.append(value)
        elif field in ("event", "id", "retry"):
            self._fields[field] = value
        return None


class StreamingTestResponse(TestResponse):
    """A response whose body is read incrementally (``client.stream(...)``).

    Status and headers are available as soon as the app sent
    ``http.response.start``. Read the body with ``iter_bytes()`` /
    ``iter_text()`` / ``iter_lines()`` / ``iter_sse()`` (sync client) or the
    ``aiter_*`` variants (:class:`~lauren.testing.AsyncTestClient`);
    :attr:`body` holds what has been read so far. ``close()`` /
    ``aclose()`` — also run when the ``with`` block exits — delivers
    ``http.disconnect`` to the app if the response is still in flight.
    """

    def __init__(
        self, status_code: int | None, headers: Iterable[tuple[str, str]], *, source: Any, **kwargs: Any
    ) -> None:
        super().__init__(status_code, headers, b"", **kwargs)
        self._source = source
        self._parts: list[bytes] = []
        self.is_stream_consumed = False
        self.is_closed = False

    @property
    def body(self) -> bytes:
        return b"".join(self._parts)

    @body.setter
    def body(self, value: bytes) -> None:
        self._parts = [value]

    def _record(self, chunk: bytes | None) -> bytes | None:
        if chunk is None:
            self.is_stream_consumed = True
            self.aborted = self._source.aborted
        else:
            self._parts.append(chunk)
        return chunk

    # -- sync iteration ------------------------------------------------------------

    def iter_bytes(self) -> Iterator[bytes]:
        """Yield body chunks as the app sends them."""
        while not self.is_stream_consumed:
            chunk = self._record(self._source.next_chunk())
            if chunk is None:
                return
            yield chunk

    def iter_raw(self) -> Iterator[bytes]:
        return self.iter_bytes()

    def iter_text(self) -> Iterator[str]:
        decoder = codecs.getincrementaldecoder(self.encoding)()
        for chunk in self.iter_bytes():
            text = decoder.decode(chunk)
            if text:
                yield text
        decoder.decode(b"", final=True)  # raises on a truncated multi-byte sequence

    def iter_lines(self) -> Iterator[str]:
        """Yield body lines without their line terminators."""
        return _iter_lines_from(self.iter_text())

    def iter_sse(self) -> Iterator["ServerSentEvent"]:
        """Yield each Server-Sent Event as a :class:`lauren.ServerSentEvent`.

        Comment-only blocks (keep-alive pings) are yielded too, with
        ``data=None``; an unterminated event at the end of the stream is
        dropped, as the HTML standard requires.
        """
        parser = _SSEParser()
        for line in self.iter_lines():
            event = parser.feed_line(line)
            if event is not None:
                yield event

    def read(self) -> bytes:
        """Read the rest of the body and return all of it."""
        for _ in self.iter_bytes():
            pass
        return self.body

    def close(self) -> None:
        """Stop reading; disconnects from the app if the response is still in flight."""
        if not self.is_closed:
            self.is_closed = True
            self._source.close()
            self.aborted = self._source.aborted

    # -- async iteration -------------------------------------------------------------

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        while not self.is_stream_consumed:
            chunk = self._record(await self._source.anext_chunk())
            if chunk is None:
                return
            yield chunk

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        async for chunk in self.aiter_bytes():
            yield chunk

    async def aiter_text(self) -> AsyncIterator[str]:
        decoder = codecs.getincrementaldecoder(self.encoding)()
        async for chunk in self.aiter_bytes():
            text = decoder.decode(chunk)
            if text:
                yield text
        decoder.decode(b"", final=True)  # raises on a truncated multi-byte sequence

    async def aiter_lines(self) -> AsyncIterator[str]:
        pending = ""
        async for text in self.aiter_text():
            lines, pending = _split_lines(pending + text, final=False)
            for line in lines:
                yield line
        lines, pending = _split_lines(pending, final=True)
        for line in lines:
            yield line
        if pending:
            yield pending

    async def aiter_sse(self) -> AsyncIterator["ServerSentEvent"]:
        parser = _SSEParser()
        async for line in self.aiter_lines():
            event = parser.feed_line(line)
            if event is not None:
                yield event

    async def aread(self) -> bytes:
        async for _ in self.aiter_bytes():
            pass
        return self.body

    async def aclose(self) -> None:
        if not self.is_closed:
            self.is_closed = True
            await self._source.aclose()
            self.aborted = self._source.aborted


__all__ = [
    "HTTPStatusAssertionError",
    "ResponseHeaders",
    "SentRequest",
    "StreamingTestResponse",
    "TestResponse",
]
