"""Actionable hints for startup errors, computed only on failure paths.

Keep the original exception object, message, code and detail keys intact. Add
optional graph-derived hints, a source location and a documentation link, then
mirror hints as PEP 678 notes so an ordinary traceback is useful too.
"""

from __future__ import annotations

import difflib
import inspect
from collections import deque
from typing import Any

from .exceptions import (
    CircularDependencyError,
    DIScopeViolationError,
    MissingProviderError,
    ModuleExportViolation,
    RouterConflictError,
    StartupError,
    UnresolvableParameterError,
)

_BASE = "https://lauren-py.dev/reference/errors/"


def _name(token: Any) -> str:
    """Describe a provider token as it appears in error detail."""
    return getattr(token, "__name__", None) or (token if isinstance(token, str) else repr(token))


def _source(target: Any) -> dict[str, Any] | None:
    """Locate the declaration, including its decorators when possible."""
    try:
        filename = inspect.getsourcefile(target)
        _, line = inspect.getsourcelines(target)
    except (TypeError, OSError):
        return None
    if filename is None:
        return None
    return {
        "file": filename,
        "line": line,
        "symbol": f"{target.__module__}.{target.__qualname__}",
    }


def _import_chain(graph: Any, consumer: type, owner: type) -> tuple[type, ...] | None:
    """Find a shortest module import path from the consumer to the owner."""
    pending: deque[tuple[type, ...]] = deque([(consumer,)])
    seen: set[type] = set()
    while pending:
        chain = pending.popleft()
        current = chain[-1]
        if current is owner:
            return chain
        if current in seen or current not in graph.modules:
            continue
        seen.add(current)
        for imported in graph.modules[current].imports:
            if isinstance(imported, type) and imported not in seen:
                pending.append((*chain, imported))
    return None


def _missing_provider(exc: MissingProviderError, graph: Any, container: Any) -> tuple[list[str], Any]:
    """Explain a missing or invisible token using declaring modules."""
    token_name = exc.detail.get("token", "")
    consumer_name = exc.detail.get("module")
    consumer = next((cls for cls in graph.modules if cls.__name__ == consumer_name), None)
    declarations = [
        (cls, token)
        for cls, mod in graph.modules.items()
        for token in mod.own_providers
        if _name(token) == token_name
    ]
    if declarations:
        owner, token = declarations[0]
        target = consumer or owner
        if consumer is not None and consumer is owner:
            return [f"Add {token_name} to {consumer.__name__}'s providers=[{token_name}]."], target
        if consumer is not None:
            chain = _import_chain(graph, consumer, owner)
            if chain is None:
                hint = (
                    f"{token_name} is declared in {owner.__name__}; add "
                    f"{owner.__name__} to {consumer.__name__}'s imports and export {token_name}."
                )
            elif token not in graph.modules[owner].exported:
                hint = (
                    f"{token_name} is declared in {owner.__name__}, which {consumer.__name__} imports, "
                    f"but {owner.__name__} does not export it — add exports=[{token_name}] "
                    f"to {owner.__name__}."
                )
            else:
                for imported in chain[1:]:
                    if imported is owner or token in graph.modules[imported].exported:
                        continue
                    hint = (
                        f"The import chain {' -> '.join(cls.__name__ for cls in chain)} stops at "
                        f"{imported.__name__}; re-export {token_name} from {imported.__name__}."
                    )
                    break
                else:
                    hint = f"Import a module that exports {token_name} into {consumer.__name__}."
            return [hint], target
        return [
            f"{token_name} is declared in {owner.__name__}; import that module and export the token."
        ], target
    names = sorted({_name(p.cls) for p in container.registered_providers()})
    close = difflib.get_close_matches(token_name, names, n=1, cutoff=0.6)
    hints = [
        f"Did you mean {close[0]}?" if close else f"Register {token_name} in a module's providers=[...]."
    ]
    return hints, consumer


def _unresolvable_parameter(
    exc: UnresolvableParameterError, graph: Any, container: Any, handler: Any, controller: Any
) -> tuple[list[str], Any]:
    """Suggest an annotation, a registered provider or the missing declaration."""
    if handler is None:
        return [], None
    if exc.detail.get("reason") is not None:
        # A resolved type failed a specific validator; don't suggest registering
        # its extractor marker as a provider.
        return [], handler
    name = exc.detail.get("param", "")
    param = inspect.signature(handler).parameters.get(name)
    if param is None:
        return [], handler
    annotation = param.annotation
    if annotation is inspect.Parameter.empty:
        return [f"Annotate parameter {name!r} with an extractor or a registered provider type."], handler
    ann_name = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))
    ann_name = ann_name.strip("\"'")
    candidate = getattr(handler, "__globals__", {}).get(ann_name)
    if isinstance(candidate, type):
        if candidate not in {p.cls for p in container.registered_providers()}:
            return [f"Add providers=[{candidate.__name__}] to the controller's module."], handler
        consumer = graph.module_for_controller(controller) if controller is not None else None
        if consumer is not None and candidate not in graph.visible_in(consumer):
            missing = MissingProviderError(detail={"token": candidate.__name__, "module": consumer.__name__})
            hints, _ = _missing_provider(missing, graph, container)
            return hints, handler
        return [f"Use an extractor marker or Depends[{candidate.__name__}] for parameter {name!r}."], handler
    names = sorted({_name(p.cls) for p in container.registered_providers()})
    close = difflib.get_close_matches(ann_name, names, n=1, cutoff=0.6)
    if close:
        return [f"Did you mean {close[0]} instead of {ann_name}?"], handler
    return [f"Register {ann_name} in the module's providers=[...] or correct its annotation."], handler


def _dependency_path(error: StartupError, container: Any) -> tuple[list[dict[str, Any]], Any]:
    """Recover named dependency edges for a cycle or scope violation."""
    providers = container.registered_providers()
    named = {_name(provider.cls): provider for provider in providers}
    cycle = error.detail.get("cycle")
    pairs = list(zip(cycle, cycle[1:])) if cycle else []
    if not pairs:
        dependency = error.detail.get("dependency")
        for provider in providers:
            if provider.scope.label != error.detail.get("dependent_scope"):
                continue
            for param, token in (*provider.deps, *provider.field_deps):
                if _name(token) == dependency:
                    pairs = [(_name(provider.cls), dependency)]
                    break
            if pairs:
                break
    edges: list[dict[str, Any]] = []
    target = None
    for parent_name, child_name in pairs:
        provider = named.get(parent_name)
        if provider is None:
            continue
        target = target or provider.factory or provider.cls
        parameter = next(
            (param for param, token in (*provider.deps, *provider.field_deps) if _name(token) == child_name),
            "<alias>" if provider.provider_kind == "existing" else "?",
        )
        edge = {"from": parent_name, "to": child_name, "parameter": parameter}
        source = _source(provider.factory or provider.cls)
        if source is not None:
            edge["source"] = source
        edges.append(edge)
    return edges, target


def enrich_startup_error(
    error: StartupError,
    *,
    graph: Any = None,
    container: Any = None,
    handler: Any = None,
    controller: Any = None,
    conflicting_handler: Any = None,
) -> StartupError:
    """Add graph-aware diagnostics to *error* and return that same object.

    Run only while a startup failure propagates, never during successful
    compilation or request dispatch. Ignore unknown error categories without
    changing their original semantics.
    """
    if "docs_url" in error.detail:
        return error
    hints: list[str] = []
    target: Any = handler or controller
    if isinstance(error, MissingProviderError) and graph is not None and container is not None:
        hints, target = _missing_provider(error, graph, container)
    elif isinstance(error, UnresolvableParameterError) and graph is not None and container is not None:
        hints, target = _unresolvable_parameter(error, graph, container, handler, controller)
    elif isinstance(error, RouterConflictError):
        if handler is not None and conflicting_handler is not None:
            second = _source(conflicting_handler)
            location = f" ({second['file']}:{second['line']})" if second else ""
            hints = [
                f"Rename one route or use a distinct method/path; existing handler: "
                f"{conflicting_handler.__qualname__}{location}."
            ]
    elif isinstance(error, ModuleExportViolation):
        name = error.detail.get("module")
        token = error.detail.get("export")
        if name and token:
            hints = [f"Declare {token} in {name}'s providers=[...] or import a module exporting it."]
        declaring = error.detail.get("modules", ())
        if declaring:
            token = error.detail.get("provider", "the provider")
            hints = [
                f"{token} is declared in {' and '.join(declaring)}; declare it once, export it there and import that module elsewhere."
            ]
        if graph is not None:
            target = next((cls for cls in graph.modules if cls.__name__ == name), target)
    elif isinstance(error, DIScopeViolationError | CircularDependencyError):
        if container is not None:
            edges, target = _dependency_path(error, container)
            if edges:
                error.detail["dependency_path"] = edges
                chain = " -> ".join(f"{edge['from']}.{edge['parameter']}" for edge in edges)
                hints.append(f"Dependency path: {chain} -> {edges[-1]['to']}.")
        if isinstance(error, DIScopeViolationError):
            hints.append("Make the consumer request-scoped or resolve the dependency within app.scope().")
        else:
            hints.append(
                "Break the dependency cycle by extracting the shared dependency into a separate provider."
            )
    if hints:
        error.detail["hints"] = hints
        for hint in hints:
            error.add_note(f"help: {hint}")
    source = _source(target) if target is not None else None
    if source is not None:
        error.detail["source"] = source
    docs_url = _BASE + "#" + error.code.replace("_", "-")
    error.detail["docs_url"] = docs_url
    error.add_note(f"docs: {docs_url}")
    return error


__all__ = ["enrich_startup_error"]
