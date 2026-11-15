"""Minimal, stdlib-only CLI for inspecting a compiled application.

The richer lauren-cli package can build tables, graphs and contract diffs on
:func:`lauren.reflect.describe_app`. These core commands only compile the
application; they never enter its lifecycle context or serve a request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence, TextIO

from ..exceptions import StartupError
from ..reflect import describe_app
from ._runner import _TargetError, load_context, render_startup_error


def _parser(action: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"python -m lauren {action}")
    parser.add_argument("-t", "--target", dest="target_option", metavar="TARGET")
    parser.add_argument("target", nargs="?", help="pkg.module:app, or set LAUREN_TARGET")
    if action == "check":
        parser.add_argument("--format", choices=("text", "json", "github"), default="text")
    elif action == "routes":
        parser.add_argument("--json", action="store_true", help="emit a JSON route array")
        parser.add_argument("--path", help="filter by path substring")
        parser.add_argument("--method", help="filter by HTTP method or WS")
        parser.add_argument("--module", help="filter by owning module")
        parser.add_argument("--with-chain", action="store_true", help="show guards and middleware")
    elif action == "openapi":
        parser.add_argument("--out", metavar="PATH", help="write the document instead of stdout")
    return parser


def _github_escape(value: Any, *, property_value: bool = False) -> str:
    """Escape an untrusted diagnostic field for GitHub workflow commands."""
    text = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if property_value else text


def _error_payload(exc: StartupError) -> dict[str, Any]:
    return {"code": exc.code, "message": exc.message, "detail": exc.detail}


def _report_failure(exc: BaseException, action: str, output_format: str, out: TextIO, err: TextIO) -> int:
    if isinstance(exc, _TargetError):
        err.write(f"error: {exc}\n")
        return 2
    if isinstance(exc, StartupError):
        if output_format == "json":
            out.write(json.dumps({"ok": False, "error": _error_payload(exc)}, sort_keys=True) + "\n")
        elif output_format == "github":
            source = exc.detail.get("source", {})
            filename = source.get("file", "")
            if filename:
                try:
                    filename = str(Path(filename).resolve().relative_to(Path.cwd().resolve()))
                except ValueError:
                    pass
            filename = _github_escape(filename, property_value=True)
            line = source.get("line", 1)
            hints = "; ".join(exc.detail.get("hints", ()))
            msg = f"{exc.code}: {exc.message}" + (f"; help: {hints}" if hints else "")
            out.write(f"::error file={filename},line={line}::{_github_escape(msg)}\n")
        else:
            err.write(render_startup_error(exc, action="build"))
        return 1
    if isinstance(exc, (ImportError, AttributeError)):
        err.write(f"error: cannot import app: {type(exc).__name__}: {exc}\n")
        return 2
    err.write(render_startup_error(exc, action="build"))
    return 1


async def _run(action: str, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    target = args.target_option or args.target or os.environ.get("LAUREN_TARGET")
    if not target:
        err.write(f"python -m lauren {action}: error: missing TARGET (pkg.module:attr)\n")
        return 2
    try:
        context = await load_context(target)
    except Exception as exc:
        return _report_failure(exc, action, getattr(args, "format", "text"), out, err)
    app = context.app
    if action == "check":
        # The factory has validated the whole graph; no context.__aenter__.
        if args.format == "json":
            out.write(json.dumps({"ok": True, "routes": len(app.routes())}) + "\n")
        else:
            out.write(f"OK: {len(app.routes())} compiled routes, lifecycle not started\n")
        return 0
    if action == "routes":
        manifest = describe_app(app)
        routes: list[dict[str, Any]] = [asdict(route) for route in manifest.routes]
        routes.extend(
            {
                "method": "WS",
                "path": gateway.path,
                "name": None,
                "handler": gateway.cls,
                "module": gateway.module,
                "guards": gateway.guards,
                "middlewares": gateway.middlewares,
                "interceptors": gateway.interceptors,
                "events": len(gateway.events),
            }
            for gateway in manifest.websocket_gateways
        )
        if args.path:
            routes = [route for route in routes if args.path in route["path"]]
        if args.method:
            routes = [route for route in routes if route["method"] == args.method.upper()]
        if args.module:
            routes = [route for route in routes if args.module in (route["module"] or "")]
        if args.json:
            out.write(json.dumps(routes, sort_keys=True, ensure_ascii=False) + "\n")
        else:
            out.write("METHOD  PATH  NAME  HANDLER  GUARDS  MODULE\n")
            for route in routes:
                handler = route["handler"].rsplit(".", 2)[-2:]
                display = ".".join(handler)
                guards = ",".join(guard.rsplit(".", 1)[-1] for guard in route["guards"])
                owner = (route["module"] or "-").rsplit(".", 1)[-1]
                out.write(
                    f"{route['method']:<7} {route['path']:<24} {route['name'] or '-':<16} "
                    f"{display:<28} {guards or '-':<16} {owner}\n"
                )
                if args.with_chain:
                    chain = (*route["middlewares"], *route["guards"], *route["interceptors"])
                    out.write(f"        chain: {' -> '.join(chain) if chain else '-'}\n")
        return 0
    spec = app.openapi()
    serialized = json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(serialized, encoding="utf-8")
        out.write(f"wrote {args.out}\n")
    else:
        out.write(serialized)
    return 0


def inspect_main(action: str, argv: Sequence[str], stdout: TextIO, stderr: TextIO) -> int:
    """Run a core inspection action and return its exit code."""
    tokens = list(argv)
    if action == "openapi" and tokens and tokens[0] == "export":
        tokens.pop(0)
    parser = _parser(action)
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            args = parser.parse_args(tokens)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    return asyncio.run(_run(action, args, stdout, stderr))


__all__ = ["inspect_main"]
