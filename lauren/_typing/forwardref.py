"""ForwardRef & string-annotation resolver used by lauren's startup phases.

This module is a trimmed, framework-flavoured descendant of the
standalone ``forwardref_evaluator`` reference implementation. It has been
reshaped to match three invariants that the rest of lauren already
respects:

1. **Never raise during resolution.** Startup code paths
   (:func:`lauren._di._safe_type_hints`,
   :func:`lauren._asgi._safe_type_hints`) are explicitly allowed to
   produce a *partial* hint dict — the framework then raises a crisp
   ``UnresolvableParameterError`` at a higher layer where the parameter
   name and owning class are already in scope.  To keep that property
   the resolver defaults to :attr:`ResolutionStrategy.LENIENT`: it
   returns the best-effort result and silently falls back to the
   original ForwardRef when a name cannot be resolved.

2. **Preserve ``Annotated`` metadata.** Every extractor in
   :mod:`lauren.extractors` rides on ``Annotated[...]``; stripping it
   would break ``Path[int]``, ``Depends[Svc]``, pipes, and discriminator
   metadata. The resolver always walks generics with
   ``include_extras=True`` semantics.

3. **Build a rich namespace on demand.** For a given function or class
   we combine: builtins, ``typing``, ``typing_extensions`` (if present),
   ``collections.abc``, the owner's module globals, and — crucially for
   self-referential classes — the class itself keyed by its own
   ``__name__``.  Call-site locals may be layered on top via an
   ``extra_localns`` dict.

The public API is :func:`resolve_type_hints` and
:func:`resolve_forwardref`; everything else is considered private.
"""

from __future__ import annotations

import builtins
import collections.abc as _abc
import sys
import typing
from enum import Enum
from typing import Annotated, Any, Callable, ForwardRef, Union, get_args, get_origin


# ---------------------------------------------------------------------------
# Errors & configuration
# ---------------------------------------------------------------------------


class ForwardRefError(Exception):
    """Base class for forward-reference resolution failures.

    Only raised when the caller explicitly opts into
    :attr:`ResolutionStrategy.STRICT`. Every call-site in lauren proper
    uses :attr:`ResolutionStrategy.LENIENT` and therefore never observes
    this exception.
    """


class ResolutionStrategy(Enum):
    """How the resolver handles names it cannot evaluate.

    STRICT
        Raise :class:`ForwardRefError`. Useful for unit tests or tools
        that want to surface every misspelt annotation.

    LENIENT
        Default for framework code. Returns the original
        :class:`typing.ForwardRef` (or string) unchanged so that the
        caller may decide whether the unresolved name is fatal in its
        own context.

    REPLACE_ANY
        Silently substitute :data:`typing.Any` for any name that cannot
        be resolved. Primarily intended for documentation generators.
    """

    STRICT = "strict"
    LENIENT = "lenient"
    REPLACE_ANY = "replace_any"


# ---------------------------------------------------------------------------
# Namespace construction
# ---------------------------------------------------------------------------


def _typing_namespace() -> dict[str, Any]:
    """Collect every public name exported by ``typing`` (and
    ``typing_extensions`` if installed) plus ``collections.abc``.

    Cached via module-level lookup — Python caches attribute access on
    modules so the overhead per call is negligible, but we still assemble
    the dict lazily on first use.
    """
    ns: dict[str, Any] = {}
    for mod in (typing,):
        for name in dir(mod):
            if not name.startswith("_"):
                ns[name] = getattr(mod, name)
    try:
        import typing_extensions as _te

        for name in dir(_te):
            if not name.startswith("_"):
                ns.setdefault(name, getattr(_te, name))
    except ImportError:  # pragma: no cover - typing_extensions is optional
        pass
    for name in dir(_abc):
        if not name.startswith("_"):
            ns.setdefault(name, getattr(_abc, name))
    return ns


_TYPING_NS: dict[str, Any] | None = None


def _cached_typing_ns() -> dict[str, Any]:
    global _TYPING_NS
    if _TYPING_NS is None:
        _TYPING_NS = _typing_namespace()
    return _TYPING_NS


def _owner_globals(owner: Any) -> dict[str, Any]:
    """Extract the module globals for a function or class owner.

    Mirrors the probing the stdlib's ``get_type_hints`` performs: use
    ``__globals__`` for functions, and walk ``sys.modules`` for classes
    (which do not carry that attribute). Returns an empty dict when no
    source module can be located.
    """
    if owner is None:
        return {}
    # Functions and methods carry their definition-site globals directly.
    globs = getattr(owner, "__globals__", None)
    if isinstance(globs, dict):
        return dict(globs)
    module_name = getattr(owner, "__module__", None)
    if module_name:
        module = sys.modules.get(module_name)
        if module is not None:
            return dict(getattr(module, "__dict__", {}))
    return {}


def _build_namespace(
    owner: Any,
    *,
    extra_globals: dict[str, Any] | None,
    extra_localns: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compose the evaluation namespace for ``owner``.

    Layering (later layers override earlier ones):

    1. Python builtins.
    2. ``typing`` / ``typing_extensions`` / ``collections.abc``.
    3. ``owner``'s module globals.
    4. For classes: the class itself under ``cls.__name__`` so that
       self-referential annotations like ``left: "Node"`` inside
       ``class Node`` resolve to the enclosing class.
    5. Explicit ``extra_globals`` supplied by the caller.
    6. Explicit ``extra_localns`` supplied by the caller — highest
       priority, used for method-local names (e.g. a class or type alias
       defined inside a pytest function body).
    """
    ns: dict[str, Any] = {}
    ns.update(vars(builtins))
    # Owner globals come **before** the typing layer so that any name
    # the user actually imported at module level wins over a same-named
    # typing construct (``Counter``, ``Deque``, ``List``, ``Set``, ...).
    # Without this ordering, a user class named ``Counter`` that lives
    # in a nested scope would be silently resolved to ``typing.Counter``
    # — a subtle correctness bug that would fool the lenient path.
    ns.update(_owner_globals(owner))
    if isinstance(owner, type):
        ns.setdefault(owner.__name__, owner)
    # Typing layer is a *fallback* — only fills gaps the owner did not.
    for key, value in _cached_typing_ns().items():
        ns.setdefault(key, value)
    if extra_globals:
        ns.update(extra_globals)
    if extra_localns:
        ns.update(extra_localns)
    return ns


# ---------------------------------------------------------------------------
# Recursive evaluation
# ---------------------------------------------------------------------------


def _as_string(ref: Any) -> str | None:
    """Reduce a :class:`ForwardRef` or ``str`` to its source string."""
    if isinstance(ref, ForwardRef):
        return ref.__forward_arg__
    if isinstance(ref, str):
        return ref
    return None


def _rebuild_generic(original: Any, origin: Any, new_args: tuple[Any, ...]) -> Any:
    """Reconstruct a generic ``origin[args]`` with resolved ``new_args``.

    Special-cases every generic form lauren can encounter: ``Union``,
    :pep:`604` unions, ``Annotated``, ``Literal``, ``ClassVar``,
    ``Final``, ``Callable`` (which has its own split shape), and the
    regular parametric generics (``list``, ``dict``, ``tuple``, ...).
    Returns ``original`` unchanged for shapes we cannot rebuild — the
    resolver is always best-effort.
    """
    if origin is Union:
        return Union[new_args]  # type: ignore[valid-type]

    # PEP 604 unions — X | Y | Z — live on ``types.UnionType`` at
    # runtime. They can be rebuilt with repeated ``|``.
    if sys.version_info >= (3, 10):
        import types as _types

        if isinstance(original, _types.UnionType):
            result = new_args[0]
            for extra in new_args[1:]:
                result = result | extra
            return result

    if origin is typing.Literal:
        return typing.Literal[new_args]  # type: ignore[valid-type]
    if origin is typing.ClassVar:
        return typing.ClassVar[new_args[0]]  # type: ignore[valid-type]
    if origin is typing.Final:
        final_arg = new_args[0]
        if isinstance(final_arg, list):
            final_arg = tuple(final_arg)
        elif isinstance(final_arg, set):
            final_arg = frozenset(final_arg)
        return typing.Final[final_arg]  # type: ignore[valid-type]

    # ``Annotated[T, meta1, meta2, ...]`` — we always preserve the
    # metadata so extractor markers and pipes survive resolution.
    if origin is not None and _is_annotated(original):
        return Annotated[tuple(new_args)]  # type: ignore[valid-type]

    if origin is _abc.Callable:
        # Callable[[a, b], r] has a 2-tuple arg shape.
        if len(new_args) >= 1 and isinstance(new_args[0], list):
            return _abc.Callable[new_args[0], new_args[-1]]  # type: ignore[index]
        return original

    if origin is not None:
        try:
            return origin[new_args]  # type: ignore[index]
        except TypeError:
            # Some origins (PEP 585 specials, etc.) refuse subscripting;
            # fall back to the object's own ``copy_with`` hook if it
            # exposes one, and otherwise leave the original untouched.
            copier = getattr(original, "copy_with", None)
            if callable(copier):
                try:
                    return copier(new_args)
                except Exception:  # pragma: no cover
                    return original
            return original
    return original


def _mark_if_from_typing_only(
    original: Any,
    resolved: Any,
    owner_globals: dict[str, Any],
) -> Any:
    """Tag a resolved hint as ``ForwardRef`` when its root name is
    sourced purely from the ``typing`` fallback layer.

    Rationale
    ---------
    The lenient namespace layers ``typing`` under the owner globals so
    that well-known typing names (``List``, ``Dict``, ``Optional``)
    keep working even when the user did not import them. But some of
    those names — ``Counter``, ``Deque``, ``OrderedDict`` — can
    accidentally shadow *user* classes that live in a nested scope and
    therefore aren't in module globals. Silently resolving to the
    typing construct would hide the problem.

    This helper detects the specific case where an annotation string
    names a simple identifier that:

    * isn't present in the owner's module globals,
    * but *is* present in the typing namespace.

    In that case we keep the value as a :class:`ForwardRef` so the
    outer fallback (``_safe_type_hints``) knows to retry with the
    calling-frame stack merged in.
    """
    text = _as_string(original)
    if text is None or not text.isidentifier():
        return resolved
    if text in owner_globals:
        return resolved
    if text in _cached_typing_ns():
        return ForwardRef(text)
    return resolved


def _is_annotated(ann: Any) -> bool:
    """True when ``ann`` is an ``Annotated[...]`` value.

    We can't use ``get_origin`` on its own because it returns the first
    metadata *type* rather than ``Annotated``; the actual
    ``__metadata__`` attribute is the reliable marker.
    """
    return hasattr(ann, "__metadata__") and hasattr(ann, "__origin__")


class _Resolver:
    """Stateful recursive evaluator.

    Owns:

    * the composed namespace dict (``ns``),
    * a per-invocation ``_resolving`` set that detects cycles the same
      way :func:`typing.get_type_hints` does internally,
    * the selected :class:`ResolutionStrategy`.

    The resolver is intentionally a throwaway object — build one per
    top-level call to :func:`resolve_type_hints`. Caching across calls
    would introduce lifetime hazards (e.g. resolved classes pinned
    forever) that are not worth the cost, given type-hint resolution is
    a startup-time concern in lauren.
    """

    def __init__(
        self,
        ns: dict[str, Any],
        strategy: ResolutionStrategy,
        max_depth: int,
    ) -> None:
        self.ns = ns
        self.strategy = strategy
        self.max_depth = max_depth
        self._resolving: set[str] = set()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def resolve(self, ann: Any, depth: int = 0) -> Any:
        if depth > self.max_depth:
            return self._fallback(ann, str(ann))
        if isinstance(ann, (str, ForwardRef)):
            return self._resolve_ref(ann, depth)
        return self._walk_generic(ann, depth)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_ref(self, ref: Any, depth: int) -> Any:
        text = _as_string(ref)
        if text is None:  # pragma: no cover - defensive
            return ref
        if text in self._resolving:
            # Cycles can legally occur for mutually recursive types;
            # returning the original ForwardRef lets the outer generic
            # keep its shape while the inner position stays a ref.
            return ForwardRef(text)
        self._resolving.add(text)
        try:
            try:
                evaluated = eval(text, self.ns, None)  # noqa: S307
            except NameError:
                return self._fallback(ref, text)
            except (SyntaxError, TypeError, AttributeError):
                return self._fallback(ref, text)
            return self._walk_generic(evaluated, depth + 1)
        finally:
            self._resolving.discard(text)

    def _walk_generic(self, ann: Any, depth: int) -> Any:
        origin = get_origin(ann)
        args = get_args(ann)
        if not args:
            return ann
        new_args = tuple(self.resolve(a, depth) for a in args)
        if new_args == args:
            return ann
        return _rebuild_generic(ann, origin, new_args)

    def _fallback(self, original: Any, text: str) -> Any:
        if self.strategy is ResolutionStrategy.STRICT:
            raise ForwardRefError(f"Cannot resolve forward reference {text!r}")
        if self.strategy is ResolutionStrategy.REPLACE_ANY:
            return Any
        # LENIENT — return a fresh ForwardRef so callers can still spot
        # that the value was unresolved, without losing the original
        # source text.
        if isinstance(original, ForwardRef):
            return original
        return ForwardRef(text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_forwardref(
    ref: ForwardRef | str,
    *,
    owner: Any = None,
    globalns: dict[str, Any] | None = None,
    localns: dict[str, Any] | None = None,
    strategy: ResolutionStrategy = ResolutionStrategy.LENIENT,
    max_depth: int = 16,
) -> Any:
    """Resolve a single :class:`ForwardRef` or source string.

    ``owner`` may be a function, method, class, or module — whichever
    one "owns" the annotation. Its module globals (and, for classes,
    the class itself) are layered into the evaluation namespace so that
    self-referential strings resolve automatically.

    Unresolvable references are handled according to ``strategy``: see
    :class:`ResolutionStrategy` for semantics.
    """
    ns = _build_namespace(
        owner,
        extra_globals=globalns,
        extra_localns=localns,
    )
    return _Resolver(ns, strategy, max_depth).resolve(ref)


def resolve_type_hints(
    obj: Callable[..., Any] | type,
    *,
    globalns: dict[str, Any] | None = None,
    localns: dict[str, Any] | None = None,
    include_extras: bool = True,
    strategy: ResolutionStrategy = ResolutionStrategy.LENIENT,
    max_depth: int = 16,
) -> dict[str, Any]:
    """Drop-in replacement for :func:`typing.get_type_hints`.

    Differences from the stdlib helper:

    * **Tolerant** by default — unresolved references are returned as
      :class:`ForwardRef` instances rather than raising ``NameError``.
      Callers that treat unresolved annotations as fatal can escalate
      the strategy to :attr:`ResolutionStrategy.STRICT`.

    * ``Annotated`` metadata is preserved by default
      (``include_extras=True``). This keeps ``Path[int]``,
      ``Depends[Svc]`` and pipe markers alive through resolution —
      a property the extractor compiler relies on.

    * For classes, annotations are collected across the whole MRO and
      the class itself is injected into the evaluation namespace so
      ``left: "Node"`` inside ``class Node`` resolves without the
      caller providing ``localns``.

    * Never walks the interpreter frame stack. Callers that want
      function-local names to participate must pass them in via
      ``localns``; this avoids the brittle frame-crawling the previous
      ``_safe_type_hints`` helpers depended on.
    """
    # First try the fast path: the stdlib resolver is already remarkably
    # capable and ~10x faster. Only fall through to the custom walker
    # when it raises (unresolvable ForwardRef, missing name, etc.).
    #
    # NOTE: NameError from the stdlib is NOT swallowed silently — when
    # we fall through to the lenient walker we mark the corresponding
    # annotation with a :class:`ForwardRef` so the caller's
    # ``_has_unresolved`` check can trigger a retry with a wider
    # namespace (typically the calling frame stack). This preserves the
    # invariant that "name missing at module scope" always reaches the
    # outer fallback, rather than being silently shadowed by an
    # unrelated same-named construct in ``typing``.
    stdlib_failed = False
    try:
        import typing as _typing

        return _typing.get_type_hints(
            obj,
            globalns=globalns,
            localns=localns,
            include_extras=include_extras,
        )
    except NameError:
        stdlib_failed = True
    except Exception:
        pass

    ns = _build_namespace(
        obj,
        extra_globals=globalns,
        extra_localns=localns,
    )
    resolver = _Resolver(ns, strategy, max_depth)

    # Gather raw annotations across the MRO for classes, or from
    # ``__annotations__`` directly for functions / methods.
    raw: dict[str, Any] = {}
    if isinstance(obj, type):
        for base in reversed(obj.__mro__):
            raw.update(getattr(base, "__annotations__", {}) or {})
    else:
        raw.update(getattr(obj, "__annotations__", {}) or {})

    # When the stdlib fast path raised ``NameError`` we want to surface
    # that signal to the caller: resolve the known-bad names as
    # :class:`ForwardRef` rather than picking up unrelated fallbacks
    # (e.g. ``typing.Counter`` for a user class ``Counter``). We do this
    # by refusing to accept a fallback value that came from a key the
    # owner's module globals did not contain.
    owner_globals = _owner_globals(obj)
    resolved: dict[str, Any] = {}
    for name, ann in raw.items():
        resolved[name] = resolver.resolve(ann)
        if stdlib_failed:
            resolved[name] = _mark_if_from_typing_only(ann, resolved[name], owner_globals)
        if not include_extras and _is_annotated(resolved[name]):
            # Mirror the stdlib behaviour when extras are not wanted.
            resolved[name] = get_args(resolved[name])[0]
    return resolved


__all__ = [
    "ForwardRefError",
    "ResolutionStrategy",
    "resolve_forwardref",
    "resolve_type_hints",
]
