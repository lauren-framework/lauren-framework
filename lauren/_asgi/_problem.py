"""RFC 9457 problem-details rendering helpers (private).

:func:`problem_payload` builds the ``error_format="rfc9457"`` body; the
``rfc7807`` (legacy) and default envelopes are built in
:func:`lauren._asgi._error_response`.

The ``errors`` member is an **array** of validation entries, following the
convention of RFC 9457 §3's example (``{"detail", "pointer"}``). Entries are
derived from the error's structured ``detail``:

* ``detail["issues"]`` — a list of ``{"loc", "type", "msg"}`` mappings (the
  aggregated validation-issue format). Any :class:`~lauren.HTTPError` that
  carries such a list gets an ``errors`` array, so a validation engine only
  has to raise an error with ``issues`` for problem details to pick them up.
* otherwise, for an :class:`~lauren.ExtractorError` whose ``detail`` names a
  ``field`` — today's extractor failures — one entry per message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from ..exceptions import ExtractorError, LaurenError

#: Characters left unescaped in a URI fragment (RFC 3986 ``pchar`` minus ``/``).
_FRAGMENT_SAFE = "!$&'()*+,;=:@-._~"
#: Characters left unescaped in the ``instance`` path reference.
_PATH_SAFE = "/:@!$&'()*+,;=-._~"
#: ``loc`` roots that name a request parameter rather than a body location.
_PARAMETER_ROOTS: frozenset[str] = frozenset({"path", "query", "header", "cookie", "form"})


def json_pointer(parts: Sequence[Any]) -> str:
    """Return the RFC 6901 JSON Pointer for ``parts`` as a URI fragment (``#/a/0/b``).

    ``~`` and ``/`` inside a token are escaped as ``~0`` and ``~1``; characters
    not allowed in a URI fragment are percent-encoded. No parts gives ``#``
    (the whole document).
    """
    tokens = (str(part).replace("~", "~0").replace("/", "~1") for part in parts)
    return "#" + "".join("/" + quote(token, safe=_FRAGMENT_SAFE) for token in tokens)


def instance_reference(path: str) -> str:
    """The ``instance`` member for a request path: a relative URI reference."""
    return quote(path, safe=_PATH_SAFE)


def _issue_entry(issue: Mapping[str, Any]) -> dict[str, Any]:
    """Map one ``{"loc", "type", "msg"}`` issue to an RFC 9457 ``errors`` entry."""
    entry: dict[str, Any] = {"detail": str(issue.get("msg", ""))}
    raw_loc = issue.get("loc") or ()
    loc = list(raw_loc) if isinstance(raw_loc, (list, tuple)) else [raw_loc]
    if loc:
        root, rest = str(loc[0]), loc[1:]
        if root == "body":
            entry["pointer"] = json_pointer(rest)
        elif root in _PARAMETER_ROOTS and rest:
            entry["in"] = root
            entry["parameter"] = str(rest[0])
            if len(rest) > 1:
                entry["pointer"] = json_pointer(rest[1:])
        else:
            entry["in"] = root
            entry["pointer"] = json_pointer(rest)
    issue_type = issue.get("type")
    if issue_type:
        entry["code"] = str(issue_type)
    return entry


def validation_errors(err: LaurenError, *, title: str) -> list[dict[str, Any]] | None:
    """The RFC 9457 ``errors`` array for ``err``, or ``None`` when it has none."""
    detail = err.detail
    if not isinstance(detail, Mapping):
        return None
    issues = detail.get("issues")
    if isinstance(issues, (list, tuple)) and issues:
        return [_issue_entry(issue) for issue in issues if isinstance(issue, Mapping)] or None
    if not isinstance(err, ExtractorError) or "field" not in detail:
        return None
    raw = detail.get("errors")
    if isinstance(raw, (list, tuple)) and raw:
        messages = [str(message) for message in raw]
    elif isinstance(raw, str) and raw:
        messages = [raw]
    else:
        messages = [err.message or title]
    constraint = detail.get("constraint")
    entries: list[dict[str, Any]] = []
    for message in messages:
        entry: dict[str, Any] = {"detail": message, "parameter": str(detail["field"])}
        if constraint:
            entry["code"] = str(constraint)
        entries.append(entry)
    return entries


def problem_payload(
    err: LaurenError,
    *,
    status: int,
    code: str,
    title: str,
    instance: str | None,
    type_base: str | None,
) -> dict[str, Any]:
    """Build the RFC 9457 problem-details object for ``err``.

    ``type`` is the error's ``problem_type`` when set, else ``type_base + code``
    when the app configured a base, else ``"about:blank"`` (RFC 9457 §3.1.1).
    Lauren's structured ``detail`` dict rides in the ``context`` extension
    member; validation failures add an ``errors`` array.
    """
    problem_type = getattr(err, "problem_type", None) or (
        f"{type_base}{code}" if type_base else "about:blank"
    )
    payload: dict[str, Any] = {
        "type": problem_type,
        "title": title,
        "status": status,
        "detail": err.message or title,
    }
    if instance is not None:
        payload["instance"] = instance
    payload["code"] = code
    if err.detail:
        payload["context"] = err.detail
    errors = validation_errors(err, title=title)
    if errors:
        payload["errors"] = errors
    return payload
