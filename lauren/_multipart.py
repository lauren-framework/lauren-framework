"""Minimal ``multipart/form-data`` parser for the ``UploadFile`` extractor.

lauren does not ship a full-blown multipart parser as a public feature;
this module is a private helper exposing exactly the operations the
:class:`~lauren.extractors.UploadFile` extractor needs:

* Extract the ``boundary`` from a ``Content-Type`` header.
* Split a buffered body into its constituent parts.
* Expose each part's ``Content-Disposition`` (``name`` / ``filename``)
  and ``Content-Type`` plus the raw bytes.

Design goals and trade-offs
---------------------------

The parser is intentionally small and *strict*: it rejects malformed
inputs rather than guessing. It expects the body to fit in memory
(matching the behaviour of :meth:`Request.form`); a future
``StreamingMultipart`` path could build on top, but the 80% case for
web forms is small-to-medium files that buffered parsing handles
well.

What we do NOT implement:

* Nested ``multipart/mixed`` parts (rare, only seen in legacy APIs).
* The obsolete ``multipart/form-data; boundary-quoted`` form where a
  quoted boundary contained characters requiring escaping; in
  practice every modern client emits a plain token.
* Charset negotiation for non-UTF-8 part values \u2014 non-file fields
  are decoded as UTF-8; file fields stay as ``bytes`` so the caller
  can choose.

These gaps are documented in :class:`UploadFile`'s docstring so
users know the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .exceptions import ExtractorError


# A multipart boundary is always preceded by CRLF plus ``--``; a final
# boundary is suffixed with an extra ``--``. The constants below mirror
# RFC 7578 exactly so the parser can use them verbatim.
_CRLF = b"\r\n"
_CRLF_CRLF = b"\r\n\r\n"


@dataclass
class _Part:
    """A single parsed multipart part.

    Attributes are the minimum required by :class:`UploadFile`:

    * ``name`` \u2014 the ``name`` attribute of the part's
      ``Content-Disposition`` header, i.e. the form field name.
      Always present in a well-formed request; a missing ``name``
      indicates a malformed body.
    * ``filename`` \u2014 the ``filename`` attribute if present. Parts
      without a filename are plain form fields; parts with one are
      file uploads.
    * ``content_type`` \u2014 the part's declared media type, defaulting
      to ``text/plain`` when absent (per RFC 7578 \u00a74.4).
    * ``data`` \u2014 the raw body bytes of the part. The surrounding
      CRLFs have already been stripped.
    * ``headers`` \u2014 every header line as a ``(name-lowercase, value)``
      tuple so ``Content-Transfer-Encoding`` and friends remain
      accessible to advanced callers without us having to parse them
      exhaustively up front.
    """

    name: str
    filename: str | None
    content_type: str
    data: bytes
    headers: list[tuple[str, str]]


def parse_boundary(content_type: str) -> str:
    """Extract the ``boundary`` parameter from a ``Content-Type`` header.

    Raises :class:`ExtractorError` when the header is absent, malformed,
    or does not announce a multipart body \u2014 callers can propagate
    that exception directly to the client as a 422.
    """
    if not content_type:
        raise ExtractorError(
            "missing Content-Type header",
            detail={"expected": "multipart/form-data; boundary=..."},
        )
    # Split the header into media-type and parameters. The media-type
    # must begin with ``multipart/`` (we accept ``multipart/form-data``,
    # ``multipart/related``, ``multipart/mixed``; the framework only
    # uses form-data but the parser is agnostic).
    lower = content_type.lower()
    if not lower.startswith("multipart/"):
        raise ExtractorError(
            "Content-Type is not multipart",
            detail={"content_type": content_type},
        )
    # Locate ``boundary=...`` \u2014 case-insensitive key, value may be
    # either bare or quoted. Strip trailing semicolon-separated
    # parameters so we don't pick up unrelated tokens.
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            value = piece[len("boundary=") :]
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            if not value:
                raise ExtractorError(
                    "empty multipart boundary",
                    detail={"content_type": content_type},
                )
            return value
    raise ExtractorError(
        "multipart Content-Type missing boundary",
        detail={"content_type": content_type},
    )


def iter_parts(body: bytes, boundary: str) -> Iterable[_Part]:
    """Yield every :class:`_Part` found in ``body`` using ``boundary``.

    The parser is a linear scan: it walks the body looking for the
    boundary delimiter and yields each inter-delimiter region as a
    ``_Part``. Malformed input (missing final boundary, bad header
    block, unterminated disposition) raises :class:`ExtractorError`.
    """
    delim = b"--" + boundary.encode("ascii")
    # The first occurrence of ``--boundary`` is the preamble separator;
    # everything before it is ignored (per RFC 7578 \u00a74.1).
    idx = body.find(delim)
    if idx == -1:
        raise ExtractorError(
            "multipart body missing opening boundary",
            detail={"boundary": boundary},
        )
    cursor = idx + len(delim)
    while True:
        # After a boundary, two bytes decide the next state:
        # ``--`` \u2192 final boundary; no more parts.
        # ``\r\n`` \u2192 next part's header block begins.
        if cursor + 2 > len(body):
            raise ExtractorError("truncated multipart body")
        peek = body[cursor : cursor + 2]
        if peek == b"--":
            # Final boundary \u2014 stop iterating.
            return
        if peek != _CRLF:
            raise ExtractorError(
                "malformed multipart boundary",
                detail={"at_offset": cursor},
            )
        cursor += 2  # skip CRLF
        # Locate the blank line that terminates the header block.
        hdr_end = body.find(_CRLF_CRLF, cursor)
        if hdr_end == -1:
            raise ExtractorError("multipart part missing header terminator")
        header_block = body[cursor:hdr_end].decode("utf-8", errors="replace")
        data_start = hdr_end + len(_CRLF_CRLF)
        # Find the next boundary marker \u2014 that's where this part ends.
        next_delim = body.find(_CRLF + delim, data_start)
        if next_delim == -1:
            raise ExtractorError(
                "multipart part missing trailing boundary",
                detail={"boundary": boundary},
            )
        part_body = body[data_start:next_delim]
        yield _build_part(header_block, part_body)
        cursor = next_delim + len(_CRLF) + len(delim)


def _build_part(header_block: str, data: bytes) -> _Part:
    """Parse a header block plus body into a :class:`_Part`."""
    headers: list[tuple[str, str]] = []
    name: str | None = None
    for raw_line in header_block.split("\r\n"):
        if not raw_line:
            continue
        if ":" not in raw_line:
            raise ExtractorError(
                "malformed multipart header line",
                detail={"line": raw_line},
            )
        name, _, value = raw_line.partition(":")
        headers.append((name.strip().lower(), value.strip()))
    # Extract disposition fields (name / filename).
    disposition = _header_value(headers, "content-disposition")
    if disposition is None:
        raise ExtractorError("multipart part missing Content-Disposition")
    params = _parse_header_parameters(disposition)
    name = params.get("name")
    if name is None:
        raise ExtractorError(
            "multipart part Content-Disposition missing name",
            detail={"disposition": disposition},
        )
    filename = params.get("filename")
    content_type = _header_value(headers, "content-type") or "text/plain"
    return _Part(
        name=name,
        filename=filename,
        content_type=content_type,
        data=data,
        headers=headers,
    )


def _header_value(headers: list[tuple[str, str]], name: str) -> str | None:
    for k, v in headers:
        if k == name:
            return v
    return None


def _parse_header_parameters(value: str) -> dict[str, str]:
    """Parse a ``; key=value; key=value`` header into a dict.

    Values may be bare tokens or double-quoted strings with simple
    backslash-escaping \u2014 the minimum spec required by RFC 7578. We
    deliberately do NOT support RFC 2231 charset/language encoded
    parameters; those are vanishingly rare in form uploads and the
    parser stays much smaller without them.
    """
    out: dict[str, str] = {}
    pieces = value.split(";")
    # Skip the media-type / disposition token itself.
    for piece in pieces[1:]:
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        k, _, v = piece.partition("=")
        k = k.strip().lower()
        v = v.strip()
        if v.startswith('"') and v.endswith('"'):
            # Unescape backslash-prefixed characters.
            inner = v[1:-1]
            unescaped: list[str] = []
            i = 0
            while i < len(inner):
                c = inner[i]
                if c == "\\" and i + 1 < len(inner):
                    unescaped.append(inner[i + 1])
                    i += 2
                    continue
                unescaped.append(c)
                i += 1
            v = "".join(unescaped)
        out[k] = v
    return out


__all__ = [
    "_Part",
    "iter_parts",
    "parse_boundary",
]
