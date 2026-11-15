"""Bounded incremental multipart framing and quote-aware MIME parameters.

The parser emits headers, bounded data chunks and part-end events. Storage and
field policies live in the form runtime. ``iter_parts`` retains the buffered
private helper for callers that already own a complete byte string.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from urllib.parse import unquote_to_bytes

from .exceptions import ExtractorError, RequestBodyTooLarge

_CRLF = b"\r\n"
_CRLF_CRLF = b"\r\n\r\n"
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")


@dataclass
class _Part:
    name: str
    filename: str | None
    content_type: str
    data: bytes
    headers: list[tuple[str, str]]


def _malformed(message: str, **detail: object) -> ExtractorError:
    return ExtractorError.from_issues(
        [{"loc": ["body"], "type": "body_invalid", "msg": message}],
        field="body",
        message=message,
        detail={"reason": "malformed_body", **detail},
    )


def parse_boundary(content_type: str) -> str:
    """Extract a bounded ASCII boundary, including a quoted parameter value."""
    if not content_type:
        raise _malformed("missing Content-Type header", expected="multipart/form-data; boundary=...")
    if not content_type.lower().startswith("multipart/"):
        raise _malformed("Content-Type is not multipart", content_type=content_type)
    boundary = _parse_header_parameters(content_type).get("boundary")
    if boundary is None:
        raise _malformed("multipart Content-Type missing boundary", content_type=content_type)
    if not boundary:
        raise _malformed("empty multipart boundary", content_type=content_type)
    if len(boundary) > 70 or not boundary.isascii() or any(ord(c) < 32 or ord(c) > 126 for c in boundary):
        raise _malformed("multipart boundary must be at most 70 printable ASCII characters")
    if boundary.endswith(" "):
        raise _malformed("multipart boundary cannot end with a space")
    return boundary


def _parameter_pieces(value: str) -> list[str]:
    pieces: list[str] = []
    start = 0
    quoted = escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            pieces.append(value[start:index])
            start = index + 1
    if quoted or escaped:
        raise _malformed("unterminated quoted multipart parameter")
    pieces.append(value[start:])
    return pieces


def _unquote_parameter(value: str) -> str:
    if not value.startswith('"'):
        return value
    out: list[str] = []
    index = 1
    while index < len(value):
        char = value[index]
        if char == '"':
            if value[index + 1 :].strip():
                raise _malformed("unexpected data after quoted multipart parameter")
            return "".join(out)
        if char == "\\":
            index += 1
            if index == len(value):
                break
            char = value[index]
        out.append(char)
        index += 1
    raise _malformed("unterminated quoted multipart parameter")


def _percent_bytes(value: str) -> bytes:
    if _BAD_PERCENT.search(value):
        raise _malformed("invalid percent escape in extended filename")
    return unquote_to_bytes(value)


def _extended_filename(parts: list[tuple[bool, str]]) -> str:
    if not parts[0][0]:
        if any(encoded for encoded, _ in parts):
            raise _malformed("encoded filename continuation needs a charset")
        return "".join(value for _, value in parts)
    first = parts[0][1].split("'", 2)
    if len(first) != 3 or not first[0]:
        raise _malformed("extended filename needs charset'language'value")
    charset, _, first_value = first
    try:
        chunks = [_percent_bytes(first_value)]
        for encoded, value in parts[1:]:
            chunks.append(_percent_bytes(value) if encoded else value.encode("ascii"))
        return b"".join(chunks).decode(charset)
    except (UnicodeError, LookupError) as exc:
        raise _malformed("invalid extended filename encoding") from exc


def _parse_header_parameters(value: str) -> dict[str, str]:
    """Parse quoted parameters and prefer RFC 5987/2231 filename extensions."""
    out: dict[str, str] = {}
    for piece in _parameter_pieces(value)[1:]:
        key, separator, raw = piece.strip().partition("=")
        if separator:
            out[key.strip().lower()] = _unquote_parameter(raw.strip())
    if "filename*" in out:
        out["filename"] = _extended_filename([(True, out["filename*"])])
    else:
        segments: dict[int, tuple[bool, str]] = {}
        for key, raw in out.items():
            if not key.startswith("filename*"):
                continue
            suffix = key[len("filename*") :]
            encoded = suffix.endswith("*")
            number = suffix[:-1] if encoded else suffix
            if number.isascii() and number.isdigit():
                if len(number) > 5:
                    raise _malformed("invalid filename continuation index")
                segments[int(number)] = (encoded, raw)
        if segments:
            if sorted(segments) != list(range(len(segments))):
                raise _malformed("filename continuation has a missing segment")
            out["filename"] = _extended_filename([segments[i] for i in range(len(segments))])
    return out


def _header_value(headers: list[tuple[str, str]], name: str) -> str | None:
    return next((value for key, value in headers if key == name), None)


def _build_part(header_block: str, data: bytes) -> _Part:
    headers: list[tuple[str, str]] = []
    for line in header_block.split("\r\n"):
        if not line:
            continue
        key, separator, value = line.partition(":")
        if (
            not separator
            or not _HEADER_TOKEN.fullmatch(key)
            or any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in value)
        ):
            raise _malformed("malformed multipart header line", line=line)
        key = key.lower()
        if key in ("content-disposition", "content-type") and _header_value(headers, key) is not None:
            raise _malformed("duplicate multipart header", header=key)
        headers.append((key, value.strip()))
    disposition = _header_value(headers, "content-disposition")
    if disposition is None:
        raise _malformed("multipart part missing Content-Disposition")
    if disposition.split(";", 1)[0].strip().lower() != "form-data":
        raise _malformed("multipart part disposition must be form-data")
    params = _parse_header_parameters(disposition)
    if "name" not in params:
        raise _malformed("multipart part Content-Disposition missing name", disposition=disposition)
    return _Part(
        name=params["name"],
        filename=params.get("filename"),
        content_type=_header_value(headers, "content-type") or "text/plain",
        data=data,
        headers=headers,
    )


class _MultipartParser:
    """Emit part/data/end events while retaining only a delimiter/header tail."""

    def __init__(self, boundary: str, *, max_header_bytes: int = 16 * 1024) -> None:
        self._delimiter = b"--" + boundary.encode("ascii")
        self._marker = _CRLF + self._delimiter
        self._buffer = bytearray()
        self._state = "preamble"
        self._start = True
        self._max_header_bytes = max_header_bytes
        self._header_scan = 0
        self._padding_seen = 0

    def _suffix(self, index: int, final: bool) -> tuple[int, int]:
        """Return invalid=-1, incomplete=0, next=1 or final=2 plus consumed end."""
        buffer = self._buffer
        closing = buffer[index : index + 2] == b"--"
        if closing:
            index += 2
        start = index
        index += self._padding_seen
        while index < len(buffer) and buffer[index] in (32, 9):
            index += 1
            if index - start > self._max_header_bytes:
                raise _malformed("multipart delimiter padding is too long")
        self._padding_seen = 0
        if closing and index == len(buffer) and final:
            return 2, index
        available = len(buffer) - index
        if available < 2:
            if not final and (available == 0 or buffer[index] in (13, 45)):
                self._padding_seen = index - start
                return 0, index
            return -1, index
        if buffer[index : index + 2] == _CRLF:
            return (2 if closing else 1), index + 2
        return -1, index

    def _boundary(self, final: bool, *, opening: bool) -> tuple[int, int, int]:
        cursor = 0
        if opening and self._start and self._buffer.startswith(self._delimiter):
            status, end = self._suffix(len(self._delimiter), final)
            if status >= 0:
                return 0, status, end
            cursor = 1
        while True:
            found = self._buffer.find(self._marker, cursor)
            if found < 0:
                return -1, -1, -1
            status, end = self._suffix(found + len(self._marker), final)
            if status >= 0:
                return found, status, end
            cursor = found + 1

    def _drain(self, *, final: bool) -> Iterator[tuple[str, _Part | bytes | None]]:
        while self._state != "done":
            if self._state == "headers":
                end = self._buffer.find(_CRLF_CRLF, self._header_scan)
                if end < 0:
                    if len(self._buffer) > self._max_header_bytes + 3:
                        self._headers_too_large()
                    self._header_scan = max(0, len(self._buffer) - 3)
                    break
                self._header_scan = 0
                if end > self._max_header_bytes:
                    self._headers_too_large()
                raw = bytes(self._buffer[:end])
                del self._buffer[: end + 4]
                try:
                    header = raw.decode("utf-8")
                except UnicodeDecodeError:
                    header = raw.decode("latin-1")
                self._state = "body"
                yield "part", _build_part(header, b"")
                continue
            opening = self._state == "preamble"
            found, status, end = self._boundary(final, opening=opening)
            if found >= 0 and status > 0:
                payload = bytes(self._buffer[:found]) if not opening and found else b""
                del self._buffer[:end]
                self._start = False
                self._state = "done" if status == 2 else "headers"
                if payload:
                    yield "data", payload
                if not opening:
                    yield "end", None
                if self._state == "done":
                    self._buffer.clear()
                continue
            # Preserve an incomplete delimiter; otherwise preserve only enough
            # tail to recognize a marker split across the next ASGI chunk.
            cut = found if found >= 0 else max(0, len(self._buffer) - len(self._marker) - 4)
            if cut:
                payload = bytes(self._buffer[:cut]) if not opening else b""
                del self._buffer[:cut]
                self._start = False
                if payload:
                    yield "data", payload
            break
        if final and self._state != "done":
            reason = {
                "preamble": "missing opening boundary",
                "headers": "missing header terminator",
                "body": "missing trailing boundary",
            }[self._state]
            raise _malformed(f"truncated multipart body: {reason}", state=self._state)

    def _headers_too_large(self) -> None:
        raise RequestBodyTooLarge(
            "multipart part headers exceed the configured limit",
            detail={"reason": "part_headers_too_large", "max_part_header_bytes": self._max_header_bytes},
        )

    def feed(self, data: bytes) -> Iterator[tuple[str, _Part | bytes | None]]:
        if self._state == "done":
            return
        # An ASGI server may deliver a large frame. Never copy that entire
        # frame into the framing buffer just to scan for a small delimiter.
        view = memoryview(data)
        for offset in range(0, len(view), 64 * 1024):
            self._buffer.extend(view[offset : offset + 64 * 1024])
            yield from self._drain(final=False)
            if self._state == "done":
                return

    def finish(self) -> Iterator[tuple[str, _Part | bytes | None]]:
        yield from self._drain(final=True)


def iter_parts(body: bytes, boundary: str) -> Iterable[_Part]:
    """Yield buffered parts using the same framing rules as streamed requests."""
    parser = _MultipartParser(boundary)
    part: _Part | None = None
    chunks: list[bytes] = []

    def collect(events: Iterable[tuple[str, _Part | bytes | None]]) -> Iterator[_Part]:
        nonlocal part, chunks
        for kind, value in events:
            if kind == "part":
                assert isinstance(value, _Part)
                part, chunks = value, []
            elif kind == "data":
                assert isinstance(value, bytes)
                chunks.append(value)
            else:
                assert part is not None
                part.data = b"".join(chunks)
                yield part
                part, chunks = None, []

    yield from collect(parser.feed(body))
    yield from collect(parser.finish())


__all__ = ["_Part", "iter_parts", "parse_boundary"]
