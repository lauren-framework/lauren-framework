"""Generate plain-Markdown API reference for the lauren-website.

The lauren-website renders docs with react-markdown.  The canonical
``docs/reference/*.md`` files use mkdocstrings ``:::`` directives which are
Python-only syntax that react-markdown cannot interpret.

This script uses griffe (the same parser mkdocstrings uses internally) to
extract docstrings and write clean Markdown files to
``docs/generated-reference/``.  Those files are committed to the repo so the
website's production build works without requiring Python.

Usage::

    python scripts/generate_api_docs.py

Run this script whenever docstrings change, then commit the output.

Requirements:
    griffe>=1.0   (available via ``pip install griffe`` or the docs extras)
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "docs" / "generated-reference"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Page definitions: output filename → list of "module.SymbolName" identifiers
# in the order they should appear.  Section headings are preserved from the
# mkdocstrings reference pages.
# ---------------------------------------------------------------------------

PAGES: dict[str, list[str | tuple[str, str]]] = {
    "app.md": [
        (
            "# App & Factory\n\nThe top-level entry points for creating and running a Lauren application.",
            "",
        ),
        "lauren.LaurenFactory",
        "lauren.Lauren",
        "lauren.LaurenApp",
    ],
    "decorators.md": [
        (
            "# Decorators\n\nAll user-facing class and method decorators exported by the framework.",
            "",
        ),
        ("## Module system", ""),
        "lauren.module",
        "lauren.controller",
        "lauren.injectable",
        ("## HTTP route decorators", ""),
        "lauren.get",
        "lauren.post",
        "lauren.put",
        "lauren.patch",
        "lauren.delete",
        "lauren.head",
        "lauren.options",
        ("## Middleware & Guards", ""),
        "lauren.middleware",
        "lauren.use_middlewares",
        "lauren.use_guards",
        "lauren.interceptor",
        "lauren.use_interceptors",
        "lauren.exception_handler",
        "lauren.use_exception_handlers",
        ("## Lifecycle", ""),
        "lauren.post_construct",
        "lauren.pre_destruct",
        ("## Scope", ""),
        "lauren.Scope",
    ],
    "extractors.md": [
        (
            "# Extractors\n\nTyped extractors for decomposing HTTP requests into strongly-typed Python values.",
            "",
        ),
        ("## Path, Query, Header, Cookie", ""),
        "lauren.Path",
        "lauren.Query",
        "lauren.Header",
        "lauren.Cookie",
        ("## Body extractors", ""),
        "lauren.Json",
        "lauren.Form",
        "lauren.Bytes",
        "lauren.ByteStream",
        "lauren.UploadFile",
        ("## Dependency injection extractor", ""),
        "lauren.Depends",
        ("## Pipes", ""),
        "lauren.pipe",
        "lauren.Pipe",
        "lauren.PipeContext",
        ("## Lower-level API", ""),
        "lauren.ExtractionMarker",
        "lauren.StateExtractor",
        "lauren.FieldDescriptor",
    ],
    "websockets.md": [
        (
            "# WebSockets\n\nFirst-class WebSocket support via `@ws_controller` gateways.",
            "",
        ),
        ("## Gateway decorators", ""),
        "lauren.ws_controller",
        "lauren.on_connect",
        "lauren.on_message",
        "lauren.on_disconnect",
        "lauren.on_error",
        ("## WebSocket object", ""),
        "lauren.WebSocket",
        ("## Broadcast", ""),
        "lauren.BroadcastGroup",
        ("## Socket.IO", ""),
        "lauren.socketio_controller",
        "lauren.on_socketio_event",
        "lauren.SocketIOConnection",
        ("## Exceptions", ""),
        "lauren.WebSocketError",
        "lauren.WebSocketDisconnect",
        "lauren.WebSocketValidationError",
        "lauren.WebSocketRouteNotFoundError",
    ],
    "sse.md": [
        (
            "# SSE & Streaming\n\nServer-Sent Events, typed streaming responses, and raw byte streams.",
            "",
        ),
        ("## Server-Sent Events", ""),
        "lauren.EventStream",
        "lauren.ServerSentEvent",
        "lauren.format_sse_event",
        "lauren.last_event_id",
        ("## Typed streaming", ""),
        "lauren.StreamingResponse",
        ("## Raw streams", ""),
        "lauren.Stream",
        "lauren.StreamReader",
    ],
    "di.md": [
        (
            "# Dependency Injection\n\nCustom provider recipes and DI container utilities.",
            "",
        ),
        ("## Custom providers", ""),
        "lauren.use_value",
        "lauren.use_class",
        "lauren.use_factory",
        "lauren.use_existing",
        ("## Injection helpers", ""),
        "lauren.Token",
        "lauren.Inject",
        "lauren.OptionalDep",
        ("## Container", ""),
        "lauren.DIContainer",
    ],
    "exceptions.md": [
        (
            "# Exception Catalog\n\nAll 28 typed exception classes exported by the framework.",
            "",
        ),
        ("## Base classes", ""),
        "lauren.LaurenError",
        "lauren.StartupError",
        "lauren.HTTPError",
        "lauren.LifecycleError",
        ("## Startup errors", ""),
        "lauren.RouterConflictError",
        "lauren.CircularDependencyError",
        "lauren.CircularModuleError",
        "lauren.DecoratorUsageError",
        "lauren.MissingProviderError",
        "lauren.ProtocolAmbiguityError",
        "lauren.ModuleExportViolation",
        "lauren.LifecycleConfigError",
        "lauren.MetadataInheritanceError",
        "lauren.DuplicateBindingError",
        "lauren.UnresolvableParameterError",
        "lauren.DIScopeViolationError",
        "lauren.MiddlewareConfigError",
        "lauren.GuardConfigError",
        "lauren.InterceptorConfigError",
        "lauren.ExceptionHandlerConfigError",
        "lauren.OpenAPISchemaError",
        "lauren.ExtractorError",
        "lauren.ExtractorFieldError",
        ("## HTTP errors", ""),
        "lauren.RouteNotFoundError",
        "lauren.MethodNotAllowedError",
        "lauren.RequestBodyTooLarge",
        "lauren.UnauthorizedError",
        "lauren.ForbiddenError",
        ("## Lifecycle errors", ""),
        "lauren.MissingStateError",
        "lauren.StateTypeError",
        "lauren.LifecycleViolationError",
        "lauren.DestructError",
        "lauren.DestructTimeoutError",
        "lauren.DrainTimeoutError",
    ],
    "types.md": [
        ("# Types\n\nCore request/response types and state containers.", ""),
        ("## Request & Response", ""),
        "lauren.Request",
        "lauren.Response",
        "lauren.Headers",
        "lauren.MutableHeaders",
        "lauren.ClientInfo",
        ("## State", ""),
        "lauren.State",
        "lauren.AppState",
    ],
    "background.md": [
        (
            "# Background Tasks\n\nFire-and-forget work that runs after the HTTP response has been sent.",
            "",
        ),
        "lauren.BackgroundTasks",
        "lauren.TaskHandle",
    ],
    "signals.md": [
        ("# Signals\n\nPOSIX signal integration and application shutdown hooks.", ""),
        "lauren.SignalBus",
        ("## Lifecycle events", ""),
        "lauren.LifecycleEvent",
        "lauren.StartupBegin",
        "lauren.StartupComplete",
        "lauren.RequestReceived",
        "lauren.RequestComplete",
        "lauren.ShutdownBegin",
    ],
    "testing.md": [
        (
            "# Testing\n\nIn-process ASGI test clients for unit and integration tests.",
            "",
        ),
        "lauren.testing.TestClient",
        "lauren.testing.WsTestClient",
    ],
}

# ---------------------------------------------------------------------------
# Griffe loading
# ---------------------------------------------------------------------------

try:
    import griffe
except ImportError:
    raise SystemExit(
        "griffe is required.  Install it with:\n"
        "    pip install griffe\n"
        "or use the docs extras:\n"
        "    pip install -r docs-requirements.txt"
    )


def _load_package() -> griffe.Package:
    loader = griffe.GriffeLoader(docstring_parser=griffe.Parser.google)
    return loader.load(ROOT / "lauren")


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _resolve(pkg: griffe.Package, dotted: str) -> griffe.Object | None:
    """Return the griffe object for ``lauren.Foo`` or ``lauren.module``.

    Silently returns ``None`` if the symbol is not found.
    """
    # The dotted name starts with "lauren." — strip that prefix because
    # pkg IS the lauren module; we navigate relative to it.
    if dotted.startswith("lauren."):
        dotted = dotted[len("lauren.") :]

    parts = dotted.split(".")
    obj: griffe.Object | None = pkg  # type: ignore[assignment]
    for part in parts:
        if obj is None:
            return None
        try:
            obj = obj.get_member(part)
            # Resolve aliases to their targets.
            if isinstance(obj, griffe.Alias):
                try:
                    obj = obj.target  # type: ignore[assignment]
                except Exception:
                    pass
        except Exception:
            return None
    return obj


def _format_annotation(ann: object | None) -> str:
    if ann is None:
        return ""
    try:
        return str(ann)
    except Exception:
        return ""


def _format_default(default: object | None) -> str:
    if default is None:
        return ""
    s = str(default)
    # Strip griffe Expr noise if present
    if s.startswith("<") and ">" in s:
        return ""
    return s


def _render_signature(obj: griffe.Function | griffe.Class) -> str:
    """Return a markdown code block with the signature."""
    try:
        if isinstance(obj, griffe.Function):
            params = []
            for p in obj.parameters:
                ann = _format_annotation(p.annotation)
                default = _format_default(p.default)
                part = p.name
                if ann:
                    part += f": {ann}"
                if default:
                    part += f" = {default}"
                params.append(part)
            ret = _format_annotation(obj.returns)
            sig = f"def {obj.name}({', '.join(params)})"
            if ret:
                sig += f" -> {ret}"
            return f"```python\n{sig}\n```\n"
        elif isinstance(obj, griffe.Class):
            # Show __init__ params if available
            init = obj.members.get("__init__")
            if init and isinstance(init, griffe.Function):
                params = []
                for p in init.parameters:
                    if p.name == "self":
                        continue
                    ann = _format_annotation(p.annotation)
                    default = _format_default(p.default)
                    part = p.name
                    if ann:
                        part += f": {ann}"
                    if default:
                        part += f" = {default}"
                    params.append(part)
                sig = f"class {obj.name}({', '.join(params)})"
            else:
                sig = f"class {obj.name}"
            return f"```python\n{sig}\n```\n"
    except Exception:
        pass
    return ""


def _render_docstring(obj: griffe.Object, indent: int = 0) -> str:
    """Return the docstring value as plain text (pre-formatted for Markdown)."""
    if obj.docstring is None:
        return ""
    text = obj.docstring.value.strip()
    if not text:
        return ""
    return text + "\n\n"


def _render_object(pkg: griffe.Package, dotted: str, heading_level: int = 3) -> str:
    """Render a single public API object as Markdown."""
    obj = _resolve(pkg, dotted)
    if obj is None:
        print(f"  ⚠  {dotted}: not found — skipping")
        return f"> **`{dotted}`** — symbol not found in the installed package.\n\n"

    name = obj.name
    heading = "#" * heading_level

    parts: list[str] = []
    parts.append(f"{heading} `{name}`\n\n")

    # Signature block
    if isinstance(obj, (griffe.Function, griffe.Class)):
        sig = _render_signature(obj)
        if sig:
            parts.append(sig + "\n")

    # Docstring
    parts.append(_render_docstring(obj))

    # For classes: render public methods (excluding dunder)
    if isinstance(obj, griffe.Class):
        for member_name, member in obj.members.items():
            if member_name.startswith("_"):
                continue
            if not isinstance(member, griffe.Function):
                continue
            sub_heading = "#" * (heading_level + 1)
            parts.append(f"{sub_heading} `{name}.{member_name}`\n\n")
            if isinstance(member, griffe.Function):
                sig = _render_signature(member)
                if sig:
                    parts.append(sig + "\n")
            parts.append(_render_docstring(member))

    return "".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def generate() -> None:
    print(f"Loading lauren package from {ROOT / 'lauren'} …")
    pkg = _load_package()
    print("Package loaded.  Generating reference pages …\n")

    for filename, entries in PAGES.items():
        out_path = OUTPUT_DIR / filename
        sections: list[str] = []

        for entry in entries:
            if isinstance(entry, tuple):
                # Raw Markdown heading / intro paragraph
                heading_text, _ = entry
                sections.append(heading_text + "\n\n")
            else:
                # dotted symbol name
                print(f"  {entry}")
                rendered = _render_object(pkg, entry, heading_level=3)
                sections.append(rendered)

        content = "".join(sections)
        out_path.write_text(content, encoding="utf-8")
        print(f"  → {out_path.relative_to(ROOT)}\n")

    print(f"\nDone.  Generated {len(PAGES)} files in {OUTPUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    generate()
