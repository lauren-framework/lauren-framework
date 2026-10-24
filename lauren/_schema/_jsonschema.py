"""JSON Schema 2020-12 / OpenAPI 3.1 emitter for compiled types (private).

Nodes describe themselves through ``schema(ctx)``; models, enums and backend
types register a named definition in the :class:`SchemaContext` and return a
``$ref`` to it, so recursive types terminate and shared models are emitted
once. :func:`emit` is the entry point behind ``CompiledType.json_schema``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: Supported ``dialect`` values.
DIALECTS: tuple[str, ...] = ("2020-12", "openapi-3.1")


class SchemaContext:
    """Collects named definitions while a schema is emitted."""

    def __init__(self, ref_template: str, defs: dict[str, Any] | None = None) -> None:
        #: pydantic-style template: ``{model}`` is replaced by the definition name.
        self.ref_template = ref_template
        self.defs: dict[str, Any] = defs if defs is not None else {}
        self._names: dict[Any, str] = {}
        self._owned: dict[str, Any] = {}

    def ref(self, name: str) -> dict[str, Any]:
        return {"$ref": self.ref_template.format(model=name)}

    def define(self, cls: Any, build: Callable[[], dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
        """Register ``cls`` (once) and return a ``$ref`` to its definition.

        A name already present in a caller-supplied ``components`` dict (and not
        created by this context) is assumed to describe the same type and is
        reused; two different classes sharing a name within one emission get a
        module-qualified name for the second.
        """
        name = self._names.get(cls)
        if name is not None:
            return self.ref(name)
        base = getattr(cls, "__name__", "Model")
        owner = self._owned.get(base)
        if owner is None and base in self.defs:
            self._names[cls] = base
            return self.ref(base)
        name = base
        if owner is not None:
            qualified = f"{getattr(cls, '__module__', 'model').replace('.', '_')}__{base}"
            name, n = qualified, 2
            while name in self.defs:
                name, n = f"{qualified}_{n}", n + 1
        self._names[cls] = name
        self._owned[name] = cls
        self.defs[name] = {}  # placeholder: a recursive reference finds the name
        self.defs[name] = build() if callable(build) else build
        return self.ref(name)

    def add_raw(self, name: str, schema: dict[str, Any]) -> None:
        """Add a definition produced by a backend (pydantic / msgspec ``$defs``)."""
        self.defs.setdefault(name, schema)


def emit(node: Any, *, dialect: str = "2020-12", components: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the JSON Schema for ``node``.

    ``"2020-12"`` returns a self-contained document whose definitions live under
    ``$defs``. ``"openapi-3.1"`` references ``#/components/schemas/<Name>`` and
    adds the definitions to ``components`` (a dict the caller owns, typically
    the OpenAPI document's ``components.schemas``); existing entries are kept.
    """
    if dialect not in DIALECTS:
        raise ValueError(f"unknown JSON Schema dialect {dialect!r}; use one of {', '.join(DIALECTS)}")
    if dialect == "openapi-3.1":
        ctx = SchemaContext("#/components/schemas/{model}", components if components is not None else {})
        return node.schema(ctx)
    ctx = SchemaContext("#/$defs/{model}")
    root = dict(node.schema(ctx))
    if ctx.defs:
        root["$defs"] = ctx.defs
    return root


__all__ = ["DIALECTS", "SchemaContext", "emit"]
