"""Query-string and request-body encoders for the in-process test clients.

Mirrors the ``httpx`` request API: ``params=`` (query string), ``data=``
(``application/x-www-form-urlencoded``), ``files=`` (``multipart/form-data``)
and raw ``content=``. Multipart part headers follow the WHATWG
``multipart/form-data`` encoding algorithm — ``"`` becomes ``%22`` and CR / LF
become ``%0D`` / ``%0A`` in field names and filenames, filenames are sent as
UTF-8 — so what the app receives matches what a browser would send.
"""

from __future__ import annotations

import mimetypes
import os
import secrets
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

_CRLF = b"\r\n"
_DISPOSITION_ESCAPES = {'"': "%22", "\r": "%0D", "\n": "%0A"}


def _flatten_params(params: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    """Yield ``(key, str(value))`` pairs; list / tuple values repeat the key."""
    for k, v in params.items():
        if isinstance(v, (list, tuple)):
            for item in v:
                yield k, str(item)
        else:
            yield k, str(v)


def _primitive(value: Any) -> str:
    """Render a form value the way ``httpx`` does (``True`` -> ``"true"``, ``None`` -> ``""``)."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return ""
    return str(value)


def _pairs(data: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Normalise a mapping or a list of pairs; list values repeat the key."""
    items = data.items() if isinstance(data, Mapping) else data
    out: list[tuple[str, Any]] = []
    for key, value in items:
        if isinstance(value, (list, tuple)):
            out.extend((key, item) for item in value)
        else:
            out.append((key, value))
    return out


def _file_pairs(files: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Like :func:`_pairs`, but only a *list* repeats the key — a tuple is one file spec."""
    items = files.items() if isinstance(files, Mapping) else files
    out: list[tuple[str, Any]] = []
    for key, value in items:
        if isinstance(value, list):
            out.extend((key, item) for item in value)
        else:
            out.append((key, value))
    return out


def encode_form(data: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> bytes:
    """Encode ``data`` as ``application/x-www-form-urlencoded``."""
    return urlencode([(key, _primitive(value)) for key, value in _pairs(data)]).encode("ascii")


def _escape(value: str) -> str:
    return "".join(_DISPOSITION_ESCAPES.get(ch, ch) for ch in value)


def _read_content(content: Any) -> bytes:
    if hasattr(content, "read"):
        content = content.read()
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, (bytes, bytearray, memoryview)):
        return bytes(content)
    raise TypeError(
        f"file content must be bytes, str or a readable file object, got {type(content).__name__}"
    )


def _file_part(name: str, value: Any) -> tuple[str, bytes, str, list[tuple[str, str]]]:
    """Return ``(filename, content, content_type, extra_headers)`` for one ``files=`` entry."""
    extra: list[tuple[str, str]] = []
    content_type: str | None = None
    if isinstance(value, tuple):
        if not 2 <= len(value) <= 4:
            raise TypeError(
                f"files[{name!r}] tuple must be (filename, content[, content_type[, headers]]), "
                f"got {len(value)} item(s)"
            )
        filename = value[0]
        content = value[1]
        if len(value) >= 3:
            content_type = value[2]
        if len(value) == 4:
            extra = list(dict(value[3]).items())
    else:
        content = value
        raw_name = getattr(value, "name", None)
        filename = (
            os.path.basename(str(raw_name)) if isinstance(raw_name, (str, bytes, os.PathLike)) else "upload"
        )
        if isinstance(filename, bytes):  # pragma: no cover - only for bytes paths
            filename = filename.decode("utf-8", "replace")
    filename = "upload" if filename is None else str(filename)
    if content_type is None:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return filename, _read_content(content), content_type, extra


def encode_multipart(
    data: Mapping[str, Any] | Iterable[tuple[str, Any]] | None,
    files: Mapping[str, Any] | Iterable[tuple[str, Any]],
    *,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """Encode form fields plus files as ``multipart/form-data``.

    Returns ``(body, content_type)``. ``files`` accepts ``{"f": ("a.txt",
    b"...", "text/plain")}``, ``{"f": open(path, "rb")}``, ``{"f": [(...),
    (...)]}`` (several files under one name) and the list-of-pairs form
    ``[("f", (...)), ("f", (...))]``. Plain fields come first, in order.
    """
    boundary = boundary or secrets.token_hex(16)
    delimiter = b"--" + boundary.encode("ascii")
    chunks: list[bytes] = []
    for key, value in _pairs(data or {}):
        payload = value if isinstance(value, (bytes, bytearray)) else _primitive(value).encode("utf-8")
        header = f'Content-Disposition: form-data; name="{_escape(str(key))}"'
        chunks += [delimiter, _CRLF, header.encode("utf-8"), _CRLF, _CRLF, bytes(payload), _CRLF]
    for key, value in _file_pairs(files):
        filename, content, content_type, extra = _file_part(str(key), value)
        lines = [
            f'Content-Disposition: form-data; name="{_escape(str(key))}"; filename="{_escape(filename)}"',
            f"Content-Type: {content_type}",
            *(f"{h}: {v}" for h, v in extra),
        ]
        chunks += [delimiter, _CRLF, "\r\n".join(lines).encode("utf-8"), _CRLF, _CRLF, content, _CRLF]
    chunks += [delimiter, b"--", _CRLF]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


__all__ = ["encode_form", "encode_multipart", "_flatten_params"]
