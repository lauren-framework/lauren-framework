"""Shared startup discovery pass over provider methods and classes (private).

Several features mark *provider* methods or classes with a decorator and need
the framework to find them: health checks, scheduled jobs and hosted
services, event handlers, commands, job handlers. They all need the same
thing — every method (or class) carrying a given marker on every registered
provider, found once at startup, honouring the inheritance rules. This module
is that single pass; no feature walks provider MROs on its own.

Hooking a feature in
--------------------

1. The feature's public module registers its marker when it is imported::

       register_marker(MarkerSpec("__lauren_schedule__", framework_types=(JobContext,)))

   User code can only carry the marker if it imported the feature's
   decorator, so import-time registration is enough. Registration is
   idempotent; registering a *different* spec under a known name raises
   ``ValueError``.
2. ``LaurenFactory.create`` runs Phase 6a after the final DI compile and
   before the lifecycle order is computed: it builds a :class:`DiscoveryIndex`
   for every registered marker and stores it, immutable, as ``app._discovery``.
3. The consumer reads only its markers (``app._discovery.methods[name]`` /
   ``app._discovery.classes[name]``) and runs entries through their
   precompiled :class:`CallPlan`; nothing reflects at run time.

The marker registry is process-wide by design: it records which decorators
exist (import-time declarations, append-only), never per-application data, so
apps in one process still share no state.

Rules
-----

* **Method markers** follow route discovery: walk ``cls.__mro__``, read each
  class's ``__dict__``, keep the *first* definition of each name. An override
  without the decorator therefore removes the marker. ``staticmethod`` /
  ``classmethod`` wrappers are normalised by ``_unwrap_handler_descriptor``;
  the marker may sit on either side of the wrapper.
* **Class markers** are read from the class's own ``__dict__``. A registered
  subclass that inherits a class marker without re-decorating raises
  :class:`~lauren.exceptions.MetadataInheritanceError`.
* **Order** is provider registration order (the module graph first), then
  MRO order.
* Function providers and ``use_value`` / ``use_factory`` / ``use_existing``
  providers carry no methods and are skipped; ``use_class`` providers are
  scanned on their implementation class.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

import anyio.to_thread

from ._di import (
    DIContainer,
    Provider,
    _extract_inject_token,
    _multi_binding_element_type,
    _safe_type_hints,
    _unwrap_annotated,
)
from .exceptions import (
    LifecycleViolationError,
    MetadataInheritanceError,
    MissingProviderError,
    UnresolvableParameterError,
)

if TYPE_CHECKING:
    from .context import OperationScope

_EMPTY = inspect.Parameter.empty

#: Parameter kinds a :class:`CallPlan` distinguishes.
FRAMEWORK = "framework"  # supplied by the consumer (``JobContext``, ``CommandContext``, …)
DI = "di"  # resolved through the container in the provider's owning module
ARGUMENT = "argument"  # left to the consumer (CLI arguments, event payloads, …)


# ---------------------------------------------------------------------------
# Marker declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarkerSpec:
    """How the discovery pass treats one marker attribute.

    ``kind="method"`` markers sit on functions and produce
    :class:`DiscoveredMethod` entries with a compiled :class:`CallPlan`;
    ``kind="class"`` markers sit on classes and produce ``(provider, meta)``
    pairs. ``framework_types`` lists the parameter annotations the consumer
    supplies at call time — a tuple, or a callable receiving the marker
    payload (for per-method types such as an event class).
    ``extra_params="collect"`` turns parameters that are neither framework
    values nor resolvable dependencies into :data:`ARGUMENT` entries instead
    of failing startup with :class:`~lauren.exceptions.UnresolvableParameterError`.
    """

    name: str
    kind: Literal["method", "class"] = "method"
    framework_types: tuple[Any, ...] | Callable[[Any], Iterable[Any]] = ()
    extra_params: Literal["error", "collect"] = "error"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError(f"MarkerSpec name must be a non-empty string, got {self.name!r}")
        if self.kind not in ("method", "class"):
            raise ValueError(f"MarkerSpec kind must be 'method' or 'class', got {self.kind!r}")
        if self.extra_params not in ("error", "collect"):
            raise ValueError(
                f"MarkerSpec extra_params must be 'error' or 'collect', got {self.extra_params!r}"
            )

    def framework_types_for(self, meta: Any) -> tuple[Any, ...]:
        """The framework-supplied annotation types for one marked method."""
        if callable(self.framework_types):
            return tuple(self.framework_types(meta))
        return tuple(self.framework_types)


def _as_spec(marker: MarkerSpec | str) -> MarkerSpec:
    return marker if isinstance(marker, MarkerSpec) else MarkerSpec(marker)


class MarkerRegistry:
    """An append-only set of :class:`MarkerSpec` declarations, keyed by name."""

    __slots__ = ("_specs",)

    def __init__(self, markers: Iterable[MarkerSpec | str] = ()) -> None:
        self._specs: dict[str, MarkerSpec] = {}
        for marker in markers:
            self.register(marker)

    def register(self, marker: MarkerSpec | str) -> MarkerSpec:
        """Add ``marker`` (a bare name means a method marker with default rules).

        Re-registering an equal spec is a no-op; a different spec under a known
        name raises ``ValueError`` because two features cannot share a marker.
        """
        spec = _as_spec(marker)
        existing = self._specs.get(spec.name)
        if existing is not None and existing != spec:
            raise ValueError(f"discovery marker {spec.name!r} is already registered as {existing!r}")
        self._specs.setdefault(spec.name, spec)
        return self._specs[spec.name]

    def specs(self) -> tuple[MarkerSpec, ...]:
        """Every registered spec, in registration order."""
        return tuple(self._specs.values())

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)


_REGISTRY = MarkerRegistry()


def register_marker(marker: MarkerSpec | str) -> MarkerSpec:
    """Declare a marker for the startup discovery pass (call at feature import)."""
    return _REGISTRY.register(marker)


def registered_markers() -> tuple[MarkerSpec, ...]:
    """The markers ``LaurenFactory.create`` indexes in Phase 6a."""
    return _REGISTRY.specs()


# ---------------------------------------------------------------------------
# Compiled call plans
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanParam:
    """One parameter of a discovered method and where its value comes from."""

    name: str
    kind: str  # FRAMEWORK | DI | ARGUMENT
    token: Any  # framework type, DI token, or the raw annotation for ARGUMENT
    default: Any = _EMPTY
    keyword_only: bool = False

    @property
    def has_default(self) -> bool:
        return self.default is not _EMPTY


@dataclass(frozen=True, slots=True)
class CallPlan:
    """A discovered method's parameter plan, compiled once at startup.

    Running it needs no reflection: :meth:`build_kwargs` reads framework
    values from ``values`` (then the scope's seeded values), resolves
    :data:`DI` parameters through the scope in ``owning_module``, and takes
    :data:`ARGUMENT` values from ``arguments``; :meth:`call` awaits a
    coroutine method or offloads a sync one to a worker thread.
    """

    target: str  # "Class.method", for messages
    params: tuple[PlanParam, ...]
    binding: str  # "instance" | "static" | "classmethod"
    is_coroutine: bool
    owning_module: type | None

    @property
    def needs_scope(self) -> bool:
        """``True`` when a parameter is resolved through DI (open a scope first)."""
        return any(p.kind == DI for p in self.params)

    @property
    def arguments(self) -> tuple[PlanParam, ...]:
        """The :data:`ARGUMENT` parameters the consumer must supply."""
        return tuple(p for p in self.params if p.kind == ARGUMENT)

    async def build_kwargs(
        self,
        scope: OperationScope | None = None,
        *,
        values: Mapping[Any, Any] | None = None,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Assemble the keyword arguments for one call.

        A missing value falls back to the parameter's default; without one it
        raises — :class:`~lauren.exceptions.LifecycleViolationError` for a DI
        parameter without a scope, :class:`~lauren.exceptions.MissingProviderError`
        for an unseeded framework value and
        :class:`~lauren.exceptions.UnresolvableParameterError` for a missing
        argument.
        """
        kwargs: dict[str, Any] = {}
        for param in self.params:
            if param.kind == FRAMEWORK:
                if values is not None and param.token in values:
                    kwargs[param.name] = values[param.token]
                elif scope is not None and param.token in scope.values:
                    kwargs[param.name] = scope.values[param.token]
                elif not param.has_default:
                    raise MissingProviderError(
                        f"{self.target}: no value supplied for parameter {param.name!r} "
                        f"({getattr(param.token, '__name__', param.token)})",
                        detail={
                            "target": self.target,
                            "param": param.name,
                            "reason": "missing_framework_value",
                        },
                    )
            elif param.kind == DI:
                if scope is None:
                    raise LifecycleViolationError(
                        f"{self.target}: parameter {param.name!r} is resolved through DI; "
                        "call it inside an OperationScope (app.scope())",
                        detail={"target": self.target, "param": param.name, "reason": "no_active_scope"},
                    )
                kwargs[param.name] = await scope.resolve(param.token, owning_module=self.owning_module)
            elif arguments is not None and param.name in arguments:
                kwargs[param.name] = arguments[param.name]
            elif not param.has_default:
                raise UnresolvableParameterError(
                    f"{self.target}: no value supplied for argument {param.name!r}",
                    detail={"target": self.target, "param": param.name, "reason": "missing_argument"},
                )
        return kwargs

    async def call(self, bound: Callable[..., Any], kwargs: Mapping[str, Any]) -> Any:
        """Call the bound method: await it, or run a sync method in a worker thread."""
        if self.is_coroutine:
            return await bound(**kwargs)
        return await anyio.to_thread.run_sync(functools.partial(bound, **kwargs))


def compile_call_plan(
    fn: Callable[..., Any],
    *,
    binding: str,
    target: str,
    container: DIContainer,
    owning_module: type | None,
    framework_types: tuple[Any, ...] = (),
    extra_params: str = "error",
) -> CallPlan:
    """Classify each parameter of ``fn`` once, at startup.

    Precedence matches route handlers: a ``framework_types`` annotation first,
    then ``Annotated[T, Inject(token)]``, then a type the owning module can
    resolve. Anything else becomes an :data:`ARGUMENT` when ``extra_params``
    is ``"collect"``; otherwise a parameter with a default keeps its default
    and one without fails startup with
    :class:`~lauren.exceptions.UnresolvableParameterError`. Annotations are
    resolved through ``lauren._typing.resolve_type_hints``.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise UnresolvableParameterError(
            f"cannot read the signature of {target}: {exc}",
            detail={"target": target, "reason": "no_signature"},
        ) from exc
    hints = _safe_type_hints(fn)
    items = list(signature.parameters.values())
    if binding != "static" and items and items[0].kind is not inspect.Parameter.KEYWORD_ONLY:
        items = items[1:]  # the receiver (self / cls)
    params: list[PlanParam] = []
    for param in items:
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        planned = _plan_param(
            param,
            hints.get(param.name, param.annotation),
            target=target,
            container=container,
            owning_module=owning_module,
            framework_types=framework_types,
            extra_params=extra_params,
        )
        if planned is not None:
            params.append(planned)
    return CallPlan(
        target=target,
        params=tuple(params),
        binding=binding,
        is_coroutine=inspect.iscoroutinefunction(fn),
        owning_module=owning_module,
    )


def _plan_param(
    param: inspect.Parameter,
    ann: Any,
    *,
    target: str,
    container: DIContainer,
    owning_module: type | None,
    framework_types: tuple[Any, ...],
    extra_params: str,
) -> PlanParam | None:
    """Classify one parameter; ``None`` means "leave it to its default"."""

    def _fail(reason: str, message: str, **extra: Any) -> UnresolvableParameterError:
        return UnresolvableParameterError(
            f"{target}: {message}",
            detail={"target": target, "param": param.name, "reason": reason, **extra},
        )

    if param.kind is inspect.Parameter.POSITIONAL_ONLY:
        raise _fail(
            "positional_only", f"parameter {param.name!r} is positional-only; make it a normal parameter"
        )
    has_default = param.default is not _EMPTY
    keyword_only = param.kind is inspect.Parameter.KEYWORD_ONLY
    bare = _unwrap_annotated(ann)
    framework_type = _match_framework_type(bare, framework_types)
    if framework_type is not None:
        return PlanParam(param.name, FRAMEWORK, framework_type, param.default, keyword_only)
    inject_token = _extract_inject_token(ann)
    if inject_token is not None:
        if container.has_provider(inject_token, owning_module=owning_module):
            return PlanParam(param.name, DI, inject_token, param.default, keyword_only)
        if has_default:
            return None
        raise _fail(
            "missing_provider",
            f"no provider for {_describe_token(inject_token)} (parameter {param.name!r}) is visible from its module",
            token=_describe_token(inject_token),
        )
    if (
        ann is not _EMPTY
        and _looks_like_di_token(bare)
        and container.has_provider(bare, owning_module=owning_module)
    ):
        return PlanParam(param.name, DI, bare, param.default, keyword_only)
    if extra_params == "collect":
        return PlanParam(param.name, ARGUMENT, ann, param.default, keyword_only)
    if has_default:
        return None
    raise _fail(
        "unresolvable",
        f"cannot resolve parameter {param.name!r}; annotate it with a provider visible from "
        "the provider's module or with a framework-supplied type",
    )


def _match_framework_type(ann: Any, framework_types: tuple[Any, ...]) -> Any | None:
    """Return the framework type ``ann`` names (itself or a subclass), else ``None``."""
    for candidate in framework_types:
        if ann is candidate:
            return candidate
        if isinstance(ann, type) and isinstance(candidate, type) and issubclass(ann, candidate):
            return candidate
    return None


def _looks_like_di_token(token: Any) -> bool:
    return isinstance(token, type) or callable(token) or _multi_binding_element_type(token) is not None


def _describe_token(token: Any) -> str:
    return str(getattr(token, "__name__", token))


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveredMethod:
    """A marked method on a registered provider, ready to call."""

    marker: str  # e.g. "__lauren_schedule__"
    meta: Any  # the marker payload (as stored on the function)
    provider: Provider  # container provider (token, scope, owning_module)
    attr_name: str
    raw_descriptor: Any  # from klass.__dict__; bound with __get__ like routes
    plan: CallPlan
    owner: type  # the class scanned (the implementation class for use_class)
    function: Callable[..., Any]  # the underlying function

    def bind(self, instance: Any) -> Callable[..., Any]:
        """Bind the method to ``instance`` through the descriptor protocol."""
        bound: Callable[..., Any] = self.raw_descriptor.__get__(instance, self.owner)
        return bound

    async def invoke(
        self,
        instance: Any,
        scope: OperationScope | None = None,
        *,
        values: Mapping[Any, Any] | None = None,
        arguments: Mapping[str, Any] | None = None,
    ) -> Any:
        """Build the arguments from the plan and call the method on ``instance``."""
        kwargs = await self.plan.build_kwargs(scope, values=values, arguments=arguments)
        return await self.plan.call(self.bind(instance), kwargs)


@dataclass(frozen=True, slots=True)
class DiscoveryIndex:
    """Immutable result of the discovery pass, stored on the app as ``_discovery``.

    Every requested marker has a key, with an empty tuple when nothing
    carries it.
    """

    methods: Mapping[str, tuple[DiscoveredMethod, ...]]  # marker -> entries (stable order)
    classes: Mapping[str, tuple[tuple[Provider, Any], ...]]  # class marker -> (provider, meta)

    @classmethod
    def empty(cls) -> DiscoveryIndex:
        return cls(methods=MappingProxyType({}), classes=MappingProxyType({}))

    def methods_for(self, marker: str) -> tuple[DiscoveredMethod, ...]:
        """Entries for a method marker (empty when the marker was not indexed)."""
        return self.methods.get(marker, ())

    def classes_for(self, marker: str) -> tuple[tuple[Provider, Any], ...]:
        """``(provider, meta)`` pairs for a class marker (empty when not indexed)."""
        return self.classes.get(marker, ())


def build_discovery_index(container: DIContainer, markers: Iterable[MarkerSpec | str]) -> DiscoveryIndex:
    """Scan every registered provider once for ``markers`` (startup only).

    A bare name is a method marker with default plan rules. Raises
    :class:`~lauren.exceptions.MetadataInheritanceError` for an inherited
    class marker and :class:`~lauren.exceptions.UnresolvableParameterError`
    for a marked method whose parameters cannot be planned.
    """
    specs = MarkerRegistry(markers).specs()
    method_specs = [s for s in specs if s.kind == "method"]
    class_specs = [s for s in specs if s.kind == "class"]
    methods: dict[str, list[DiscoveredMethod]] = {s.name: [] for s in method_specs}
    classes: dict[str, list[tuple[Provider, Any]]] = {s.name: [] for s in class_specs}
    if specs:
        resolved_by_class: dict[type, dict[str, tuple[Callable[..., Any], str, Any]]] = {}
        for provider in container.registered_providers():
            cls = _scan_target(provider)
            if cls is None:
                continue
            for spec in class_specs:
                class_meta = _own_class_marker(cls, spec.name)
                if class_meta is not None:
                    classes[spec.name].append((provider, class_meta))
            if not method_specs:
                continue
            resolved = resolved_by_class.get(cls)
            if resolved is None:
                resolved = resolved_by_class[cls] = _resolve_methods(cls)
            for attr_name, (fn, binding, raw) in resolved.items():
                for spec in method_specs:
                    meta = _read_marker(raw, fn, spec.name)
                    if meta is None:
                        continue
                    target = f"{cls.__name__}.{attr_name}"
                    plan = compile_call_plan(
                        fn,
                        binding=binding,
                        target=target,
                        container=container,
                        owning_module=provider.owning_module,
                        framework_types=spec.framework_types_for(meta),
                        extra_params=spec.extra_params,
                    )
                    methods[spec.name].append(
                        DiscoveredMethod(
                            marker=spec.name,
                            meta=meta,
                            provider=provider,
                            attr_name=attr_name,
                            raw_descriptor=raw,
                            plan=plan,
                            owner=cls,
                            function=fn,
                        )
                    )
    return DiscoveryIndex(
        methods=MappingProxyType({name: tuple(entries) for name, entries in methods.items()}),
        classes=MappingProxyType({name: tuple(entries) for name, entries in classes.items()}),
    )


def _scan_target(provider: Provider) -> type | None:
    """The class whose methods a provider exposes, or ``None`` to skip it."""
    if provider.provider_kind == "class":
        factory = provider.factory
        return factory if isinstance(factory, type) else None
    if provider.provider_kind is not None or provider.is_function_provider:
        return None  # use_value / use_factory / use_existing, function providers
    return provider.cls if isinstance(provider.cls, type) else None


def _own_class_marker(cls: type, marker: str) -> Any | None:
    """Read a class marker from ``cls``'s own ``__dict__`` (strict inheritance)."""
    own = cls.__dict__.get(marker)
    if own is not None:
        return own
    for base in cls.__mro__[1:]:
        if marker in base.__dict__:
            raise MetadataInheritanceError(
                f"{cls.__name__} inherits {marker} from {base.__name__} but is not itself "
                f"decorated. Re-decorate {cls.__name__} to opt in explicitly.",
                detail={"class": cls.__name__, "inherits_from": base.__name__, "marker": marker},
            )
    return None


def _resolve_methods(cls: type) -> dict[str, tuple[Callable[..., Any], str, Any]]:
    """``name -> (function, binding, raw descriptor)``, first MRO definition wins.

    Mirrors route discovery in ``LaurenFactory.create`` exactly, so an
    override without the decorator hides the base method's marker.
    """
    from ._asgi import _unwrap_handler_descriptor  # ``_asgi`` imports this module

    resolved: dict[str, tuple[Callable[..., Any], str, Any]] = {}
    for klass in cls.__mro__:
        for attr_name, raw in klass.__dict__.items():
            if attr_name in resolved:
                continue
            fn, binding = _unwrap_handler_descriptor(raw)
            if fn is None:
                continue
            resolved[attr_name] = (fn, binding, raw)
    return resolved


def _read_marker(raw: Any, fn: Callable[..., Any], marker: str) -> Any | None:
    """Read ``marker`` from ``fn`` and from any descriptor wrapping it.

    A decorator may run above ``@staticmethod`` / ``@classmethod`` (the marker
    lands on the descriptor) or below it (on the function). Values from both
    sides merge the way stacking does: lists concatenate (inner first, no
    duplicates), dicts merge, and otherwise the inner value wins.
    """
    wrappers: list[Any] = []
    current, seen = raw, set()
    while current is not fn and id(current) not in seen:  # same walk as _unwrap_handler_descriptor
        seen.add(id(current))
        wrappers.append(current)
        is_wrapper = isinstance(current, (staticmethod, classmethod))
        current = current.__func__ if is_wrapper else getattr(current, "__wrapped__", fn)
    value = getattr(fn, marker, None)
    for wrapper in reversed(wrappers):  # innermost wrapper first
        outer = getattr(wrapper, marker, None)
        if outer is None:
            continue
        if value is None:
            value = outer
        elif isinstance(value, list) and isinstance(outer, list):
            value = value + [item for item in outer if item not in value]
        elif isinstance(value, dict) and isinstance(outer, dict):
            value = {**value, **outer}
    return value


__all__ = [
    "ARGUMENT",
    "DI",
    "FRAMEWORK",
    "CallPlan",
    "DiscoveredMethod",
    "DiscoveryIndex",
    "MarkerRegistry",
    "MarkerSpec",
    "PlanParam",
    "build_discovery_index",
    "compile_call_plan",
    "register_marker",
    "registered_markers",
]
