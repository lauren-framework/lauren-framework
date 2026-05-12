"""Module graph construction and validation.

The module graph is the primary mechanism for **encapsulation** in lauren —
modelled after NestJS. Every ``@injectable`` provider declared inside a module
is visible only to:

1. That module's own controllers and providers, and
2. Any other module that ``imports`` this module AND for which this module
   ``exports`` that provider.

Controllers are always private to their declaring module. Providers that
a module ``imports`` from another module are visible only if the exporting
module lists them in its ``exports``.

Visibility is computed once at startup and frozen into a ``visible`` set per
module. The DI container consults that set when resolving dependencies so
that a controller in module *A* cannot accidentally reach a service declared
in module *B* unless *B* explicitly exported it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, ForwardRef, Iterable

from ..decorators import MODULE_META, ModuleMeta
from .._di.custom import CustomProvider, normalise_provider_token
from ..exceptions import (
    CircularModuleError,
    MetadataInheritanceError,
    ModuleExportViolation,
)


def _describe_token(tok: Any) -> str:
    """Pretty-print a provider token for error messages.

    Classes are rendered by their ``__name__``; non-class tokens fall
    through to ``repr`` so a string token prints as ``'CONNECTION'``
    (with the quotes that distinguish it from a class). The Token
    instance defines its own ``__name__`` already.
    """
    name = getattr(tok, "__name__", None)
    if name is not None:
        return str(name)
    return repr(tok)


def _resolve_forward_import(ref: Any, declaring_cls: type) -> type:
    """Resolve a ``ForwardRef`` or string in ``@module(imports=...)`` to the
    actual module class.

    Resolution order
    ----------------
    1. The declaring module's own globals (exact match, fastest path).
    2. All currently-loaded ``sys.modules`` entries (handles the case where
       the target module is defined in a *different* file that was never
       imported directly into the declaring module's namespace — the common
       circular-import scenario).

    The scan in step 2 rejects ambiguous matches (same class name in two
    different loaded modules) and raises ``ValueError`` with an actionable
    suggestion to use a dotted name (``"pkg.sub.ClassName"``) instead.

    Parameters
    ----------
    ref:
        A :class:`typing.ForwardRef` or plain ``str`` naming the target class.
        Dotted names (``"pkg.mod.ClassName"``) are resolved by splitting on
        the last ``.`` and looking up the parent module in ``sys.modules``.
    declaring_cls:
        The ``@module``-decorated class that contains the forward reference.
        Used to establish the local namespace for step 1.

    Raises
    ------
    ValueError
        When the name cannot be resolved or the match is ambiguous.
    """
    if isinstance(ref, type):
        return ref

    if isinstance(ref, ForwardRef):
        name: str = ref.__forward_arg__
    elif isinstance(ref, str):
        name = ref
    else:
        raise ValueError(
            f"Invalid entry in @module(imports=[...]): {ref!r}. "
            "Each entry must be a module class, ForwardRef('ClassName'), or 'ClassName'."
        )

    # Dotted name: "pkg.mod.ClassName" → split into module path + class name.
    if "." in name:
        mod_path, cls_name = name.rsplit(".", 1)
        sys_mod = sys.modules.get(mod_path)
        if sys_mod is not None:
            resolved = getattr(sys_mod, cls_name, None)
            if resolved is not None and isinstance(resolved, type):
                return resolved
        raise ValueError(
            f"{declaring_cls.__name__} references forward import {name!r} "
            f"but {mod_path!r} is not loaded or does not export {cls_name!r}. "
            "Ensure the module is imported before LaurenFactory.create() is called."
        )

    # Simple name: check the declaring class's own module globals first.
    module_name = getattr(declaring_cls, "__module__", None)
    if module_name:
        sys_mod = sys.modules.get(module_name)
        if sys_mod is not None:
            resolved = getattr(sys_mod, name, None)
            if resolved is not None and isinstance(resolved, type):
                return resolved

    # Fallback: scan every loaded module for a class with this name.
    # Snapshot sys.modules to avoid "dictionary changed size during iteration"
    # if a lazy import fires while we scan.
    candidates: list[type] = []
    seen_ids: set[int] = set()
    for sys_mod in list(sys.modules.values()):
        if sys_mod is None:
            continue
        resolved = getattr(sys_mod, name, None)
        if resolved is not None and isinstance(resolved, type) and id(resolved) not in seen_ids:
            candidates.append(resolved)
            seen_ids.add(id(resolved))

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        locations = ", ".join(getattr(c, "__module__", "?") + "." + c.__name__ for c in candidates)
        raise ValueError(
            f"{declaring_cls.__name__} has an ambiguous forward import {name!r}: "
            f"found {len(candidates)} classes with that name ({locations}). "
            "Use a dotted name such as ForwardRef('myapp.b_module.BModule') "
            "or 'myapp.b_module.BModule' to disambiguate."
        )

    raise ValueError(
        f"{declaring_cls.__name__} references forward import {name!r} that could "
        "not be resolved. Ensure the target module class is defined (and its "
        "module imported) before LaurenFactory.create() is called."
    )


@dataclass
class CompiledModule:
    cls: type
    meta: ModuleMeta
    #: All provider tokens **visible** inside this module — own providers plus
    #: anything re-exported by a transitively imported module.
    providers: set[Any] = field(default_factory=set)
    controllers: tuple[type, ...] = ()
    exported: set[Any] = field(default_factory=set)
    #: Only the provider tokens *declared* locally by this module (not imported).
    own_providers: set[Any] = field(default_factory=set)
    #: Custom-provider records (use_value / use_class / use_factory /
    #: use_existing) declared by this module, indexed by provider token.
    #: Each token maps to a *list* so that multiple multi-binding providers
    #: for the same ``provide=`` token are all preserved (a plain dict
    #: would silently keep only the last one).
    custom_providers: dict[Any, list[CustomProvider]] = field(default_factory=dict)


class ModuleGraph:
    """Result of Phase 1 — a frozen, validated module DAG."""

    def __init__(self) -> None:
        self.modules: dict[type, CompiledModule] = {}
        self.root: type | None = None
        self.all_providers: set[Any] = set()
        self.all_controllers: list[type] = []
        #: provider token -> declaring module class. Populated during compile.
        self._provider_owner: dict[Any, type] = {}
        #: controller class -> declaring module class.
        self._controller_owner: dict[type, type] = {}
        #: Token -> list of CustomProvider records, flattened across every
        #: module so the factory pipeline can register them in one
        #: pass. A list is used so that multiple multi-binding providers
        #: for the same token are all preserved.
        self._custom_providers: dict[Any, list[CustomProvider]] = {}

    def compile(self, root: type) -> None:
        self.root = root
        # DFS with cycle detection.
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[type, int] = {}
        path: list[type] = []

        def visit(mod_cls: type) -> CompiledModule:
            if mod_cls in self.modules:
                return self.modules[mod_cls]
            meta = _own_module_meta(mod_cls)
            c = color.get(mod_cls, WHITE)
            if c == GRAY:
                cycle = [m.__name__ for m in path[path.index(mod_cls) :] + [mod_cls]]
                raise CircularModuleError(
                    "Circular module import: " + " -> ".join(cycle),
                    detail={"cycle": cycle},
                )
            color[mod_cls] = GRAY
            path.append(mod_cls)

            compiled = CompiledModule(cls=mod_cls, meta=meta)
            compiled.controllers = meta.controllers
            # Walk imports first so exports are known.
            # Each entry may be a real class or a ForwardRef / string that
            # is resolved lazily here (all modules are loaded by compile time).
            imported_exports: set[Any] = set()
            for imp in meta.imports:
                resolved_imp = _resolve_forward_import(imp, mod_cls)
                sub = visit(resolved_imp)
                imported_exports |= sub.exported

            # Each entry in ``meta.providers`` is either a class /
            # function token (registered as-is) or a CustomProvider
            # (whose ``provide`` field is the public token). Normalise
            # so the export validator and the visibility set work
            # uniformly across the two shapes.
            own_tokens: set[Any] = set()
            for p in meta.providers:
                token = normalise_provider_token(p)
                own_tokens.add(token)
                if isinstance(p, CustomProvider):
                    compiled.custom_providers.setdefault(token, []).append(p)
            compiled.own_providers = set(own_tokens)
            providers: set[Any] = set(own_tokens)
            providers |= imported_exports
            compiled.providers = providers

            # Validate exports: must either be declared locally or imported.
            for exp in meta.exports:
                if exp not in own_tokens and exp not in imported_exports:
                    raise ModuleExportViolation(
                        f"{mod_cls.__name__} exports {_describe_token(exp)} "
                        "which it neither declares nor imports",
                        detail={
                            "module": mod_cls.__name__,
                            "export": _describe_token(exp),
                        },
                    )
            compiled.exported = set(meta.exports)

            path.pop()
            color[mod_cls] = BLACK
            self.modules[mod_cls] = compiled
            return compiled

        visit(root)

        # Collect flat lists for easy consumption + record ownership.
        for m in self.modules.values():
            for p in m.meta.providers:
                token = normalise_provider_token(p)
                self.all_providers.add(token)
                if isinstance(p, CustomProvider):
                    self._custom_providers.setdefault(token, []).append(p)
                # The first module that declares a provider owns it.
                # Declaring the same provider in two modules is
                # ambiguous and rejected here so errors surface at
                # startup rather than at resolution.
                if token in self._provider_owner and self._provider_owner[token] is not m.cls:
                    raise ModuleExportViolation(
                        f"Provider {_describe_token(token)} is declared in both "
                        f"{self._provider_owner[token].__name__} and {m.cls.__name__}; "
                        "a provider may only belong to one module.",
                        detail={
                            "provider": _describe_token(token),
                            "modules": [
                                self._provider_owner[token].__name__,
                                m.cls.__name__,
                            ],
                        },
                    )
                self._provider_owner[token] = m.cls
            for c in m.controllers:
                self.all_controllers.append(c)
                self._controller_owner[c] = m.cls

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def iter_providers(self) -> Iterable[Any]:
        return iter(self.all_providers)

    def iter_controllers(self) -> Iterable[type]:
        return iter(self.all_controllers)

    def module_for_provider(self, provider_cls: Any) -> type | None:
        """Return the module class that *declares* the provider token, or ``None``."""
        return self._provider_owner.get(provider_cls)

    def custom_providers_for(self, token: Any) -> list[CustomProvider]:
        """Return all :class:`CustomProvider` records for ``token`` (may be empty).

        Returns a list to support multi-binding scenarios where the same
        ``provide=`` token has multiple custom providers registered.
        """
        return self._custom_providers.get(token, [])

    def module_for_controller(self, controller_cls: type) -> type | None:
        """Return the module class that declares ``controller_cls``, or ``None``."""
        return self._controller_owner.get(controller_cls)

    def visible_in(self, module_cls: type) -> frozenset[Any]:
        """Return the frozen set of provider tokens visible to ``module_cls``.

        The visible set contains the module's own providers, its own
        controllers, and everything re-exported by a transitively imported
        module. Controllers are always private to their declaring module —
        they are never exported.
        """
        compiled = self.modules.get(module_cls)
        if compiled is None:
            return frozenset()
        # Own controllers are visible inside their module (e.g. when a
        # controller depends on another controller via Depends or DI), but
        # never visible to other modules.
        return frozenset(compiled.providers | set(compiled.controllers))


def _own_module_meta(cls: type) -> ModuleMeta:
    """Return ``cls``'s OWN @module metadata or raise.

    Subclasses that inherit ``__lauren_module__`` via Python's MRO without
    being re-decorated are rejected: the contract must be explicit.
    """
    own = cls.__dict__.get(MODULE_META)
    if own is not None:
        return own
    for base in cls.__mro__[1:]:
        if MODULE_META in base.__dict__:
            raise MetadataInheritanceError(
                f"{cls.__name__} inherits @module metadata from "
                f"{base.__name__} but is not itself decorated with @module. "
                "Decorate the subclass explicitly to opt in.",
                detail={"class": cls.__name__, "inherits_from": base.__name__},
            )
    raise ValueError(f"{cls.__name__} is not decorated with @module")


__all__ = ["ModuleGraph", "CompiledModule"]
