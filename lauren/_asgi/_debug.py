"""Development error pages (private).

Imported only when an app is built with ``debug=`` enabled, so apps without
debug pages never load this module. Everything here runs on the error path
only: :func:`collect` reads the traceback (``traceback`` / ``linecache``) of an
exception that already escaped every handler, and :func:`render_html` builds a
self-contained page (inline CSS, no JavaScript) in which every dynamic string
goes through :func:`html.escape`.
"""

from __future__ import annotations

import html
import importlib
import ipaddress
import linecache
import platform
import reprlib
import sys
import traceback
from pathlib import Path
from types import TracebackType
from typing import Any

from ..exceptions import StartupError
from ..types import DebugConfig, Request

_REDACTED = "[redacted]"
#: Longest exception chain (``__cause__`` / ``__context__``) shown.
_MAX_CHAIN = 8
#: Cap on any single rendered string (messages, header values, source lines).
_MAX_TEXT = 4000
#: Directory of the ``lauren`` package — frames inside it are marked as framework frames.
_LAUREN_DIR = str(Path(__file__).resolve().parent.parent)

_repr = reprlib.Repr()
_repr.maxstring = 200
_repr.maxother = 200
_repr.maxlist = _repr.maxtuple = _repr.maxdict = _repr.maxset = 10


# ---------------------------------------------------------------------------
# Configuration (startup)
# ---------------------------------------------------------------------------


def resolve_config(value: object) -> DebugConfig:
    """Normalise ``LaurenFactory.create(debug=...)`` to a validated :class:`DebugConfig`."""
    if value is True:
        config = DebugConfig()
    elif isinstance(value, DebugConfig):
        config = value
    else:
        raise StartupError(
            f"debug= expects a bool or a DebugConfig, got {type(value).__name__}",
            detail={"reason": "invalid_debug_config", "got": type(value).__name__},
        )
    for name in ("include_4xx", "allow_remote", "include_locals"):
        if not isinstance(getattr(config, name), bool):
            _invalid(name, getattr(config, name), "a bool")
    for name, minimum in (("source_context", 0), ("max_frames", 1)):
        number = getattr(config, name)
        if isinstance(number, bool) or not isinstance(number, int) or number < minimum:
            _invalid(name, number, f"an int >= {minimum}")
    for name in ("redact_headers", "redact_query_keys"):
        names = getattr(config, name)
        if isinstance(names, (str, bytes)) or not all(isinstance(n, str) for n in names):
            _invalid(name, names, "a tuple of strings")
    return config


def _invalid(field: str, value: object, expected: str) -> None:
    raise StartupError(
        f"DebugConfig({field}={value!r}) is invalid; expected {expected}",
        detail={"reason": "invalid_debug_config", "field": field, "value": repr(value)},
    )


def production_profile(container: Any) -> str | None:
    """Name of the active configuration profile when it is a production profile.

    The typed configuration module (``lauren.config``) registers a ``Profile``
    value with ``name`` and ``is_production``. Without that module, or without
    a registered profile, there is nothing to check and this returns ``None``.
    """
    try:
        config_module = importlib.import_module("lauren.config")
    except ImportError:
        return None
    profile_cls = getattr(config_module, "Profile", None)
    if profile_cls is None:
        return None
    profile = container.singletons().get(profile_cls)
    if profile is None or not getattr(profile, "is_production", False):
        return None
    return str(getattr(profile, "name", "production"))


# ---------------------------------------------------------------------------
# Request policy
# ---------------------------------------------------------------------------


def is_loopback(host: str | None) -> bool:
    """``True`` for a loopback IP address (``127.0.0.0/8``, ``::1``, IPv4-mapped loopback)."""
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def client_allowed(config: DebugConfig, request: Request) -> bool:
    """Whether ``request``'s client may see debug output."""
    if config.allow_remote:
        return True
    client = request.client
    return is_loopback(client.host if client is not None else None)


def prefers_html(accept: str | None) -> bool:
    """Whether an ``Accept`` header prefers an HTML page over JSON.

    HTML wins only when ``text/html`` (or XHTML) is listed explicitly with a
    higher quality than any JSON type; ``*/*`` alone (curl, most HTTP
    libraries) means JSON.
    """
    if not accept:
        return False
    html_q = 0.0
    json_q = 0.0
    for part in accept.split(","):
        media, _, params = part.partition(";")
        media = media.strip().lower()
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        if media in ("text/html", "application/xhtml+xml"):
            html_q = max(html_q, quality)
        elif media == "application/json" or (media.startswith("application/") and media.endswith("+json")):
            json_q = max(json_q, quality)
    return html_q > json_q


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _text(value: object) -> str:
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 — a broken __str__ must not break the page
        text = f"<unprintable {type(value).__name__}>"
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"


def _qualname(obj: object) -> str:
    if obj is None:
        return ""
    module = getattr(obj, "__module__", None)
    name = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None) or repr(obj)
    return f"{module}.{name}" if module and module != "builtins" else str(name)


def _redacted(name: str, needles: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(needle.lower() in lowered for needle in needles)


def _frame_location(tb: TracebackType | None) -> str | None:
    last = None
    for frame, lineno in traceback.walk_tb(tb):
        last = f"{frame.f_code.co_filename}:{lineno} in {frame.f_code.co_qualname}"
    return last


def _chain(exc: BaseException) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen = {id(exc)}
    current: BaseException = exc
    while len(entries) < _MAX_CHAIN:
        if current.__cause__ is not None:
            nxt, relation = current.__cause__, "cause"
        elif current.__context__ is not None and not current.__suppress_context__:
            nxt, relation = current.__context__, "context"
        else:
            break
        if id(nxt) in seen:
            break
        seen.add(id(nxt))
        entries.append(
            {
                "relation": relation,
                "type": _qualname(type(nxt)),
                "message": _text(nxt),
                "location": _frame_location(nxt.__traceback__),
            }
        )
        current = nxt
    return entries


def _frames(tb: TracebackType | None, config: DebugConfig) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for frame, lineno in list(traceback.walk_tb(tb))[-config.max_frames :]:
        code = frame.f_code
        filename = code.co_filename
        context: list[dict[str, Any]] = []
        if lineno is not None:
            for number in range(max(1, lineno - config.source_context), lineno + config.source_context + 1):
                line = linecache.getline(filename, number, frame.f_globals)
                if not line:
                    continue
                context.append(
                    {"line": number, "code": _text(line.rstrip("\n")), "current": number == lineno}
                )
        entry: dict[str, Any] = {
            "file": filename,
            "line": lineno,
            "function": code.co_qualname,
            "framework": filename.startswith(_LAUREN_DIR),
            "context": context,
        }
        if config.include_locals:
            entry["locals"] = {
                str(name): _REDACTED if _redacted(str(name), config.redact_query_keys) else _safe_repr(value)
                for name, value in frame.f_locals.items()
            }
        frames.append(entry)
    return frames


def _safe_repr(value: object) -> str:
    try:
        return _repr.repr(value)
    except Exception:  # noqa: BLE001 — a broken __repr__ must not break the page
        return f"<unrepresentable {type(value).__name__}>"


def _names(classes: Any) -> list[str]:
    return [_qualname(cls) for cls in classes]


def _route(request: Request, app: Any) -> dict[str, Any] | None:
    entry = getattr(request, "_matched_route", None)
    if entry is None:
        return None
    compiled = app._handlers.get((entry.method, entry.path_template))
    if compiled is None:
        return {"method": entry.method, "path_template": entry.path_template}
    return {
        "method": entry.method,
        "path_template": compiled.path_template,
        "handler": _qualname(compiled.handler_fn),
        "controller": _qualname(compiled.controller_cls),
        "module": _qualname(compiled.owning_module) or None,
        "middlewares": _names([*app._global_middlewares, *compiled.middleware_chain]),
        "guards": _names([*app._global_guards, *compiled.guards]),
        "interceptors": _names([*app._global_interceptors, *compiled.interceptors]),
        "exception_handlers": _names([*compiled.exception_handlers, *app._global_exception_handlers]),
        "parameters": [{"name": ext.name, "source": ext.source} for ext in compiled.extractions],
    }


def _request(request: Request, config: DebugConfig) -> dict[str, Any]:
    query = {
        key: [_REDACTED] * len(values)
        if _redacted(key, config.redact_query_keys)
        else [_text(v) for v in values]
        for key, values in request.query_params.items()
    }
    headers = [
        [name, _REDACTED if _redacted(name, config.redact_headers) else _text(value)]
        for name, value in request.headers.items()
    ]
    client = request.client
    return {
        "method": request.method,
        "path": request.path,
        "query": query,
        "headers": headers,
        "client": client.host if client is not None else None,
    }


def _environment(app: Any) -> dict[str, Any]:
    package = sys.modules.get("lauren")
    return {
        "lauren": str(getattr(package, "__version__", "unknown")),
        "python": platform.python_version(),
        "error_format": app.error_format,
    }


def collect(exc: BaseException, *, request: Request, app: Any, config: DebugConfig) -> dict[str, Any]:
    """Gather the JSON-safe debug report for an unhandled exception."""
    return {
        "exception": {"type": _qualname(type(exc)), "message": _text(exc)},
        "chain": _chain(exc),
        "frames": _frames(exc.__traceback__, config),
        "route": _route(request, app),
        "request": _request(request, config),
        "environment": _environment(app),
    }


def raised_at(exc: BaseException, config: DebugConfig) -> dict[str, Any]:
    """Where an :class:`~lauren.HTTPError` was raised, for ``DebugConfig(include_4xx=True)``."""
    frames = [
        f"{frame.f_code.co_filename}:{lineno} in {frame.f_code.co_qualname}"
        for frame, lineno in list(traceback.walk_tb(exc.__traceback__))[-config.max_frames :]
    ]
    return {"raised_at": frames[-1] if frames else None, "traceback": frames}


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSS = """
body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;color:#1f2328;background:#f6f8fa}
header{background:#9a1c1c;color:#fff;padding:20px 28px}
header h1{margin:0;font-size:20px}header p{margin:6px 0 0;white-space:pre-wrap;word-break:break-word}
main{padding:12px 28px 40px}section{background:#fff;border:1px solid #d0d7de;border-radius:6px;margin:16px 0;padding:12px 16px}
h2{font-size:15px;margin:0 0 8px}table{border-collapse:collapse;width:100%}
td,th{text-align:left;vertical-align:top;padding:3px 8px;border-top:1px solid #eaeef2;word-break:break-word}
th{width:180px;color:#57606a;font-weight:600}code,pre{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
pre{margin:6px 0 0;padding:6px 0;background:#f6f8fa;border-radius:4px;overflow-x:auto}
pre span{display:block;padding:0 8px;white-space:pre}pre span.current{background:#ffebe9;font-weight:600}
details{border-top:1px solid #eaeef2;padding:6px 0}summary{cursor:pointer}
.framework summary{color:#57606a}.muted{color:#57606a}.warn{color:#9a1c1c;font-weight:600}
"""


def _e(value: object) -> str:
    return html.escape(_text(value), quote=True)


def _rows(pairs: list[tuple[str, object]]) -> str:
    return "".join(f"<tr><th>{_e(key)}</th><td>{_e(value)}</td></tr>" for key, value in pairs)


def _frame_html(frame: dict[str, Any], *, open_: bool) -> str:
    css = ' class="framework"' if frame["framework"] else ""
    summary = f'<code>{_e(frame["function"])}</code> <span class="muted">{_e(frame["file"])}:{_e(frame["line"])}</span>'
    lines = "".join(
        f"<span{' class=current' if line['current'] else ''}>{line['line']:>5}  {_e(line['code'])}</span>"
        for line in frame["context"]
    )
    source = f"<pre>{lines}</pre>" if lines else ""
    local_vars = frame.get("locals")
    locals_html = f"<table>{_rows(sorted(local_vars.items()))}</table>" if local_vars else ""
    return (
        f"<details{css}{' open' if open_ else ''}><summary>{summary}</summary>{source}{locals_html}</details>"
    )


def render_html(info: dict[str, Any]) -> str:
    """Render the debug report as a self-contained HTML page."""
    exc = info["exception"]
    parts: list[str] = []
    chain = info["chain"]
    if chain:
        items = "".join(
            f"<tr><th>{_e(item['relation'])}</th><td><code>{_e(item['type'])}</code>: {_e(item['message'])}"
            f"{'<br><span class=muted>' + _e(item['location']) + '</span>' if item['location'] else ''}</td></tr>"
            for item in chain
        )
        parts.append(f"<section><h2>Exception chain</h2><table>{items}</table></section>")
    frames = info["frames"]
    if frames:
        rendered = "".join(
            _frame_html(frame, open_=index == len(frames) - 1) for index, frame in enumerate(frames)
        )
        parts.append(f"<section><h2>Traceback (most recent call last)</h2>{rendered}</section>")
    route = info["route"]
    if route is not None:
        pairs: list[tuple[str, object]] = [
            ("Route", f"{route['method']} {route['path_template']}"),
            ("Handler", route.get("handler", "")),
            ("Controller", route.get("controller", "")),
            ("Module", route.get("module") or ""),
            ("Middlewares", " → ".join(route.get("middlewares", [])) or "—"),
            ("Guards", " → ".join(route.get("guards", [])) or "—"),
            ("Interceptors", " → ".join(route.get("interceptors", [])) or "—"),
            ("Exception handlers", ", ".join(route.get("exception_handlers", [])) or "—"),
            (
                "Parameters",
                ", ".join(f"{p['name']} ← {p['source']}" for p in route.get("parameters", [])) or "—",
            ),
        ]
        parts.append(f"<section><h2>Route</h2><table>{_rows(pairs)}</table></section>")
    else:
        parts.append(
            '<section><h2>Route</h2><p class="muted">The request did not match a route.</p></section>'
        )
    req = info["request"]
    query = "&".join(f"{key}={value}" for key, values in req["query"].items() for value in values)
    request_pairs: list[tuple[str, object]] = [
        ("Method", req["method"]),
        ("Path", req["path"]),
        ("Query", query or "—"),
        ("Client", req["client"] or "—"),
    ]
    request_pairs.extend((name, value) for name, value in req["headers"])
    parts.append(f"<section><h2>Request</h2><table>{_rows(request_pairs)}</table></section>")
    env = info["environment"]
    env_pairs: list[tuple[str, object]] = [
        ("Lauren", env["lauren"]),
        ("Python", env["python"]),
        ("error_format", env["error_format"]),
    ]
    parts.append(f"<section><h2>Environment</h2><table>{_rows(env_pairs)}</table></section>")
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex">'
        f"<title>{_e(exc['type'])} — lauren debug</title><style>{_CSS}</style></head><body>"
        f"<header><h1>{_e(exc['type'])}</h1><p>{_e(exc['message'])}</p></header><main>"
        '<p class="warn">Debug error page — never enable debug in production.</p>'
        + "".join(parts)
        + "</main></body></html>\n"
    )
