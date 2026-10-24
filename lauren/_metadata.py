"""Typed metadata keys, typed state keys and decorator composition.

The private engine behind :class:`MetadataKey`, :class:`MetadataSetKey`,
:class:`StateKey`, :func:`compose`, :func:`provides_state` and
:func:`reject_bare_usage` (re-exported from :mod:`lauren`,
:mod:`lauren.decorators` and :mod:`lauren.types`). It imports only
:mod:`lauren.exceptions`, so every framework module may import it.

Storage stays backwards compatible: values live in ``__lauren_metadata__``
under the key's *name*, so string readers (``ctx.get_metadata("app.roles")``)
see typed values. The key objects ride alongside in
``__lauren_metadata_keys__`` (name → key), which tells the factory how each
key merges across levels. Class targets read and write their **own**
``__dict__`` (strict inheritance); functions use attribute lookup.

Nothing here runs per request except :func:`read_metadata` and
:meth:`StateKey.missing_error` — merging, collision detection and
provenance checks happen in ``LaurenFactory.create``.
"""

from __future__ import annotations

import inspect
import sys
import types as _types
import typing
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, Literal, TypeVar, Union, get_args, get_origin

from .exceptions import DecoratorUsageError, HTTPError, MissingStateError, UnresolvableParameterError

T = TypeVar("T")
_X = TypeVar("_X")

#: Name → value dict written by ``@set_metadata`` and typed keys.
SET_METADATA = "__lauren_metadata__"
#: Name → key object, recorded next to ``SET_METADATA`` by typed keys.
METADATA_KEYS = "__lauren_metadata_keys__"
#: ``compose(...)`` provenance records (a list, application order).
COMPOSED_META = "__lauren_composed__"
#: ``@provides_state(...)`` keys declared on a guard / middleware / interceptor.
PROVIDES_STATE_META = "__lauren_provides_state__"

#: Cross-level policies of a scalar :class:`MetadataKey`.
SCALAR_MERGES: tuple[str, ...] = ("override", "outer", "error")
#: Cross-level policies of a :class:`MetadataSetKey`.
SET_MERGES: tuple[str, ...] = ("union", "override")

_UNSET: Any = object()
_MISSING: Any = object()
_TYPING_FILE = getattr(typing, "__file__", "")


# ---------------------------------------------------------------------------
# reject_bare_usage
# ---------------------------------------------------------------------------


def reject_bare_usage(decorator_name: str, arg: Any) -> None:
    """Raise if a configurable decorator was used without parentheses.

    When a user writes ``@controller`` instead of ``@controller()`` Python
    passes the decorated class or function straight to the decorator's
    factory as its first positional argument, which almost always produces
    a confusing downstream error (or, worse, silently "succeeds" and
    creates a broken registration). Call this with the factory's first
    argument to surface the mistake at decoration time::

        def auth(*roles: str):
            reject_bare_usage("auth", roles[0] if roles else None)
            return compose(use_guards(JwtGuard), Roles(*roles), name="auth")
    """
    if isinstance(arg, type) or callable(arg):
        raise DecoratorUsageError(
            f"@{decorator_name} must be used with parentheses: write "
            f"'@{decorator_name}()' even when all arguments are default. "
            "The bare form is rejected because it silently binds the "
            "decorated object as the first positional configuration argument.",
            detail={
                "decorator": decorator_name,
                "target": getattr(arg, "__qualname__", repr(arg)),
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _definition_site() -> str:
    """Return ``module:line`` of the code that constructed a key (import time only)."""
    frame = sys._getframe(1)
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename != __file__ and filename != _TYPING_FILE:
            return f"{frame.f_globals.get('__name__', '?')}:{frame.f_lineno}"
        frame = frame.f_back  # type: ignore[assignment]
    return "?"  # pragma: no cover - the interpreter always has a caller frame


def _target_name(target: Any) -> str:
    return str(getattr(target, "__qualname__", None) or getattr(target, "__name__", None) or repr(target))


def _own_dict(target: Any, attr: str) -> dict[str, Any]:
    """Copy *target*'s own ``attr`` dict (``__dict__`` for classes, attribute lookup otherwise)."""
    if isinstance(target, type):
        return dict(target.__dict__.get(attr) or {})
    return dict(getattr(target, attr, None) or {})


def _store(target: Any, attr: str, value: Any) -> None:
    try:
        setattr(target, attr, value)
    except (AttributeError, TypeError):
        raise DecoratorUsageError(
            f"cannot attach metadata to {target!r}: it does not accept attributes",
            detail={"target": _target_name(target), "reason": "metadata_target"},
        ) from None


def _equal(a: Any, b: Any) -> bool:
    if a is b:
        return True
    try:
        return bool(a == b)
    except Exception:
        return False


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if type(value) is tuple:
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return (value,)


def _union(first: Iterable[Any], second: Iterable[Any]) -> tuple[Any, ...]:
    """Ordered union (first's items, then second's new ones); works for unhashable items."""
    out: list[Any] = []
    for item in (*first, *second):
        if not any(_equal(item, seen) for seen in out):
            out.append(item)
    return tuple(out)


def _declared_type(key: Any) -> Any:
    """``T`` of ``MetadataKey[T](...)`` (recorded by ``typing`` as ``__orig_class__``), or ``_UNSET``."""
    orig = getattr(key, "__orig_class__", None)
    args = get_args(orig) if orig is not None else ()
    return args[0] if args else _UNSET


def _matches(tp: Any, value: Any) -> bool | None:
    """Return whether *value* is an instance of the annotation *tp* (``None``: cannot tell)."""
    if tp is Any or tp is object:
        return True
    if tp is None or tp is type(None):
        return value is None
    origin = get_origin(tp)
    if origin is Union or isinstance(tp, _types.UnionType):
        results = [_matches(arg, value) for arg in get_args(tp)]
        if True in results:
            return True
        return None if None in results else False
    if origin is Literal:
        return any(_equal(value, choice) for choice in get_args(tp))
    if origin is typing.Annotated:
        return _matches(get_args(tp)[0], value)
    check = origin if origin is not None else tp
    if not isinstance(check, type):
        return None
    if check is int and isinstance(value, bool):
        return False
    if check is float and type(value) is int:
        return True
    try:
        return isinstance(value, check)
    except TypeError:  # non-runtime-checkable protocols and similar
        return None


def _is_decoratable(value: Any) -> bool:
    return isinstance(value, (type, _types.FunctionType, _types.MethodType, staticmethod, classmethod))


# ---------------------------------------------------------------------------
# Metadata keys
# ---------------------------------------------------------------------------


class _Frozen:
    """Immutable after ``__init__`` (``typing`` may still record ``__orig_class__``)."""

    __slots__ = ()

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "__orig_class__":
            object.__setattr__(self, name, value)
            return
        raise AttributeError(f"{type(self).__name__} objects are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} objects are immutable")


class _MetadataKeyBase(_Frozen):
    """Shared state of :class:`MetadataKey` and :class:`MetadataSetKey`."""

    __slots__ = ("name", "merge", "validate", "public", "description", "site", "__orig_class__")

    name: str
    merge: Any
    validate: bool
    public: bool
    description: str | None
    #: ``module:line`` where the key was declared (named in collision errors).
    site: str
    _allowed: tuple[str, ...] = ()

    def _setup(self, name: Any, merge: Any, validate: bool, public: bool, description: str | None) -> None:
        kind = type(self).__name__
        if not isinstance(name, str) or not name.strip():
            raise DecoratorUsageError(
                f"{kind} name must be a non-empty string, got {name!r}",
                detail={"key": repr(name), "reason": "invalid_metadata_key"},
            )
        if not callable(merge) and merge not in self._allowed:
            raise DecoratorUsageError(
                f"{kind}({name!r}, merge={merge!r}): merge must be one of "
                f"{', '.join(map(repr, self._allowed))} or a callable (outer, inner) -> value",
                detail={"key": name, "reason": "invalid_merge_policy", "merge": repr(merge)},
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "merge", merge)
        object.__setattr__(self, "validate", bool(validate))
        object.__setattr__(self, "public", bool(public))
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "site", _definition_site())

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"

    # -- decoration-time checks ---------------------------------------------

    def _check_value(self, value: Any) -> None:
        """Reject bare usage and (``validate=True``) values of the wrong type."""
        declared = _declared_type(self)
        if _is_decoratable(value) and (declared is _UNSET or _matches(declared, value) is not True):
            raise DecoratorUsageError(
                f"{self!r} was used as a bare decorator (or given {_target_name(value)} as its value); "
                "call it with the value to attach — '@key(value)'. A class or function value needs a key "
                "declared with a matching type, e.g. MetadataKey[type[Base]](...).",
                detail={"key": self.name, "reason": "bare_metadata_key", "target": _target_name(value)},
            )
        if not self.validate:
            return
        result = None if declared is _UNSET else _matches(declared, value)
        if result is None:
            raise DecoratorUsageError(
                f"{self!r} has validate=True but its type cannot be checked at runtime; declare it as "
                f"{type(self).__name__}[T](...) with a plain runtime type (bool, int, str, a class, a union of them)",
                detail={"key": self.name, "reason": "unvalidatable_metadata_key"},
            )
        if not result:
            raise DecoratorUsageError(
                f"{self!r} expects {getattr(declared, '__name__', declared)!s} values, got {value!r}",
                detail={"key": self.name, "reason": "invalid_metadata_value", "value": repr(value)},
            )

    # -- implemented by the subclasses ----------------------------------------

    def _normalize(self, value: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def _stack(self, existing: Any, value: Any, target: str) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def _merge_levels(
        self, outer: Any, inner: Any, outer_label: str, inner_label: str
    ) -> Any:  # pragma: no cover
        raise NotImplementedError


class MetadataKey(_MetadataKeyBase, Generic[T]):
    """A typed scalar route-metadata key: a decorator and a typed lookup handle.

    ``IsPublic = MetadataKey[bool]("app.is_public", default=False)`` declares
    the key; ``@IsPublic(True)`` attaches a value to a controller, route or
    WebSocket gateway; ``ctx.get_metadata(IsPublic)`` reads it back as ``bool``
    (the ``default`` when no level set it). Stacking the key twice on one
    target with different values raises :class:`DecoratorUsageError` at
    decoration time. Across levels (controller ⟵ route) ``merge`` decides:
    ``"override"`` (most specific wins), ``"outer"`` (outermost wins — pin
    security settings), ``"error"`` (conflicting levels fail
    ``LaurenFactory.create``) or a callable ``(outer, inner) -> value``.
    ``validate=True`` checks every decorated value against ``T`` at
    decoration time; ``public=True`` marks the key for manifests.
    """

    __slots__ = ("default",)
    _allowed = SCALAR_MERGES
    default: T

    def __init__(
        self,
        name: str,
        *,
        default: T,
        merge: Literal["override", "outer", "error"] | Callable[[T, T], T] = "override",
        validate: bool = False,
        public: bool = False,
        description: str | None = None,
    ) -> None:
        self._setup(name, merge, validate, public, description)
        object.__setattr__(self, "default", default)

    def __call__(self, value: T) -> Callable[[_X], _X]:
        """Return a decorator attaching *value* under this key."""
        self._check_value(value)
        return _Decoration(self, value, f"{self.name}({value!r})")

    def read(self, metadata: Mapping[str, Any]) -> T:
        """Return this key's value in *metadata* (a compiled or reflected mapping), or the default."""
        return metadata.get(self.name, self.default)  # type: ignore[no-any-return]

    def _normalize(self, value: Any) -> Any:
        return value

    def _stack(self, existing: Any, value: Any, target: str) -> Any:
        if _equal(existing, value):
            return existing
        raise DecoratorUsageError(
            f"{self!r} is applied to {target} twice with different values ({existing!r} and {value!r}); "
            "a scalar key holds one value per target — remove one of the decorators",
            detail={"key": self.name, "target": target, "reason": "metadata_conflict"},
        )

    def _merge_levels(self, outer: Any, inner: Any, outer_label: str, inner_label: str) -> Any:
        merge = self.merge
        if merge == "override":
            return inner
        if merge == "outer":
            return outer
        if merge == "error":
            if _equal(outer, inner):
                return inner
            raise DecoratorUsageError(
                f"{self!r} is declared merge='error' but {outer_label} sets {outer!r} and "
                f"{inner_label} sets {inner!r}; set it on one level only",
                detail={
                    "key": self.name,
                    "reason": "metadata_merge_conflict",
                    "outer": outer_label,
                    "inner": inner_label,
                },
            )
        return merge(outer, inner)


class MetadataSetKey(_MetadataKeyBase, Generic[T]):
    """A typed collection route-metadata key whose items accumulate.

    ``Roles = MetadataSetKey[str]("app.roles")`` declares the key;
    ``@Roles("admin", "auditor")`` adds items; ``ctx.get_metadata(Roles)``
    reads ``tuple[str, ...]`` (``()`` when no level set it). Stacked
    decorators on one target accumulate (de-duplicated, application order,
    like ``@use_guards``). Across levels ``merge="union"`` (default) gives the
    ordered union, outer level first; ``"override"`` lets the most specific
    level replace the outer items; a callable ``(outer, inner) -> tuple`` is
    also accepted.
    """

    __slots__ = ()
    _allowed = SET_MERGES

    def __init__(
        self,
        name: str,
        *,
        merge: Literal["union", "override"]
        | Callable[[tuple[T, ...], tuple[T, ...]], tuple[T, ...]] = "union",
        validate: bool = False,
        public: bool = False,
        description: str | None = None,
    ) -> None:
        self._setup(name, merge, validate, public, description)

    def __call__(self, *items: T) -> Callable[[_X], _X]:
        """Return a decorator adding *items* under this key."""
        for item in items:
            self._check_value(item)
        values = _union((), items)
        return _Decoration(self, values, f"{self.name}({', '.join(map(repr, values))})")

    def read(self, metadata: Mapping[str, Any]) -> tuple[T, ...]:
        """Return this key's items in *metadata* as a tuple (``()`` when absent)."""
        value = metadata.get(self.name)
        if value is None:
            return ()
        return _as_tuple(value)

    def _normalize(self, value: Any) -> tuple[Any, ...]:
        return _union((), _as_tuple(value))

    def _stack(self, existing: Any, value: Any, target: str) -> Any:
        return _union(_as_tuple(existing), _as_tuple(value))

    def _merge_levels(self, outer: Any, inner: Any, outer_label: str, inner_label: str) -> Any:
        merge = self.merge
        if merge == "union":
            return _union(outer, inner)
        if merge == "override":
            return inner
        return self._normalize(merge(outer, inner))


class _Decoration:
    """The decorator a typed key returns: attaches one value, returns the target unchanged."""

    __slots__ = ("_key", "_value", "_label")

    def __init__(self, key: _MetadataKeyBase, value: Any, label: str) -> None:
        self._key = key
        self._value = value
        self._label = label

    def __call__(self, target: _X) -> _X:
        stack_metadata(target, self._key, self._value)
        return target

    def __repr__(self) -> str:
        return self._label


def _collision(first: _MetadataKeyBase, second: _MetadataKeyBase, where: str) -> DecoratorUsageError:
    return DecoratorUsageError(
        f"two different metadata keys share the name {first.name!r} on {where}: {first!r} defined at "
        f"{first.site} and {second!r} defined at {second.site}; reuse one key object or rename one",
        detail={
            "key": first.name,
            "reason": "metadata_key_collision",
            "definitions": [first.site, second.site],
            "where": where,
        },
    )


def _stack_into(
    values: dict[str, Any], keys: dict[str, Any], key: _MetadataKeyBase, value: Any, where: str
) -> None:
    name = key.name
    known = keys.get(name)
    if known is not None and known is not key:
        raise _collision(known, key, where)
    values[name] = key._stack(values[name], value, where) if name in values else key._normalize(value)
    keys[name] = key


def stack_metadata(target: Any, key: Any, value: Any) -> None:
    """Attach *value* under *key* (a typed key or a legacy name) to *target*, stacking per the key."""
    values = _own_dict(target, SET_METADATA)
    keys = _own_dict(target, METADATA_KEYS)
    where = _target_name(target)
    if isinstance(key, _MetadataKeyBase):
        _stack_into(values, keys, key, value, where)
        _store(target, SET_METADATA, values)
        _store(target, METADATA_KEYS, keys)
        return
    typed = keys.get(key) if isinstance(key, str) else None
    if typed is not None:
        # A string write onto a typed key's name follows that key's stacking rule.
        _stack_into(values, keys, typed, typed._normalize(value), where)
    else:
        values[key] = value  # legacy string key: the last write wins
    _store(target, SET_METADATA, values)


def stack_metadata_dicts(
    inner_values: Mapping[str, Any],
    inner_keys: Mapping[str, Any],
    outer_values: Mapping[str, Any],
    outer_keys: Mapping[str, Any],
    where: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Combine metadata applied on both sides of a descriptor (``inner`` ran first)."""
    values = dict(inner_values)
    keys = dict(inner_keys)
    for name, value in outer_values.items():
        key = outer_keys.get(name) or keys.get(name)
        if key is None:
            values[name] = value
        else:
            _stack_into(values, keys, key, key._normalize(value), where)
    for name, key in outer_keys.items():
        known = keys.setdefault(name, key)
        if known is not key:
            raise _collision(known, key, where)
    return values, keys


def propagate_user_metadata(source: Any, target: Any) -> None:
    """``@propagate_metadata`` for ``@set_metadata`` / typed keys (the target's scalars win)."""
    src_values = _own_dict(source, SET_METADATA)
    if not src_values:
        return
    src_keys = _own_dict(source, METADATA_KEYS)
    values = _own_dict(target, SET_METADATA)
    keys = _own_dict(target, METADATA_KEYS)
    where = _target_name(target)
    merged: dict[str, Any] = {}
    for name, src_value in src_values.items():
        key = src_keys.get(name)
        known = keys.get(name)
        if key is not None and known is not None and key is not known:
            raise _collision(known, key, where)
        key = key or known
        if name not in values:
            merged[name] = key._normalize(src_value) if key is not None else src_value
        elif isinstance(key, MetadataSetKey):
            merged[name] = _union(key._normalize(src_value), key._normalize(values[name]))
        else:
            merged[name] = values[name]  # the target's own value wins
        if key is not None:
            keys[name] = key
    for name, value in values.items():
        merged.setdefault(name, value)
    _store(target, SET_METADATA, merged)
    if keys:
        _store(target, METADATA_KEYS, keys)


def own_metadata(target: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(values, keys)`` declared on *target* itself (own ``__dict__`` for classes)."""
    return _own_dict(target, SET_METADATA), _own_dict(target, METADATA_KEYS)


def merge_metadata_levels(
    levels: Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any]]],
) -> Mapping[str, Any]:
    """Merge ``(label, values, keys)`` levels, outermost first, into one read-only mapping.

    Typed keys apply their ``merge`` policy (a level that wrote the key's name
    with a plain string follows the same policy); untyped names keep the
    legacy rule — the most specific level wins. Two different key objects
    with one name raise :class:`DecoratorUsageError`. Called once per route
    at startup; the result is what ``ExecutionContext.metadata`` exposes.
    """
    merged: dict[str, Any] = {}
    owners: dict[str, _MetadataKeyBase] = {}
    labels: dict[str, str] = {}
    for label, values, keys in levels:
        for name, value in values.items():
            key = keys.get(name)
            known = owners.get(name)
            if key is not None and known is not None and key is not known:
                raise _collision(known, key, f"{labels[name]} and {label}")
            key = key or known
            if key is not None:
                owners[name] = key
                value = key._normalize(value)
                if name in merged:
                    value = key._merge_levels(key._normalize(merged[name]), value, labels[name], label)
            merged[name] = value
            labels[name] = label
    return MappingProxyType(merged)


def read_metadata(metadata: Mapping[str, Any], key: Any, default: Any = _UNSET) -> Any:
    """``get_metadata`` for every context type: typed keys read with their own default."""
    if isinstance(key, _MetadataKeyBase):
        if default is not _UNSET:
            raise TypeError(f"{key!r} carries its own default; call get_metadata(key) without one")
        return key.read(metadata)  # type: ignore[attr-defined]
    return metadata.get(key, None if default is _UNSET else default)


# ---------------------------------------------------------------------------
# State keys
# ---------------------------------------------------------------------------


class StateKey(_Frozen, Generic[T]):
    """A typed slot in ``request.state`` (and ``app_state`` / ``ws.state``).

    ``CurrentUser = StateKey[AuthUser]("app.user")`` names the slot. Guards
    and middleware write ``ctx.request.state[CurrentUser] = user``; handlers
    read ``request.state[CurrentUser]`` or declare a parameter
    ``user: Annotated[AuthUser, CurrentUser]``, compiled at startup into a
    single dict read. Values are stored under ``name``, so string readers
    (``request.state.get("app.user")``) interoperate.

    A missing value raises ``on_missing`` — :class:`MissingStateError` (500)
    by default, or an :class:`HTTPError` subclass / zero-argument factory
    (``UnauthorizedError`` for authentication keys). ``require_provider=True``
    makes ``LaurenFactory.create`` check that every route injecting the key
    has a guard, middleware or interceptor declaring
    ``@provides_state(key)`` in its chain.
    """

    __slots__ = ("name", "on_missing", "require_provider", "description", "site", "__orig_class__")

    name: str
    on_missing: Any
    require_provider: bool
    description: str | None
    site: str

    def __init__(
        self,
        name: str,
        *,
        on_missing: type[HTTPError] | Callable[[], HTTPError] | None = None,
        require_provider: bool = False,
        description: str | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise DecoratorUsageError(
                f"StateKey name must be a non-empty string, got {name!r}",
                detail={"key": repr(name), "reason": "invalid_state_key"},
            )
        problem = _on_missing_problem(on_missing)
        if problem is not None:
            raise DecoratorUsageError(
                f"StateKey({name!r}, on_missing={on_missing!r}): {problem}",
                detail={"key": name, "reason": "invalid_on_missing"},
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "on_missing", on_missing)
        object.__setattr__(self, "require_provider", bool(require_provider))
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "site", _definition_site())

    def __repr__(self) -> str:
        return f"StateKey({self.name!r})"

    def missing_error(self, param: str | None = None) -> HTTPError:
        """Build the error a missing value raises (``on_missing``, else :class:`MissingStateError`)."""
        detail: dict[str, Any] = {"key": self.name}
        if param is not None:
            detail["param"] = param
        message = f"required state {self.name!r} is missing"
        factory = self.on_missing
        if factory is None:
            return MissingStateError(message, detail=detail)
        if isinstance(factory, type):
            return factory(message, detail=detail)  # type: ignore[no-any-return]
        error = factory()
        if not isinstance(error, HTTPError):
            raise TypeError(
                f"on_missing factory of {self!r} returned {type(error).__name__}, expected an HTTPError instance"
            )
        return error


def _on_missing_problem(on_missing: Any) -> str | None:
    if on_missing is None:
        return None
    if isinstance(on_missing, type):
        return (
            None
            if issubclass(on_missing, HTTPError)
            else f"{on_missing.__name__} is not an HTTPError subclass"
        )
    if isinstance(on_missing, BaseException):
        return "an exception instance cannot be raised for every request; use the class or a zero-argument factory"
    if not callable(on_missing):
        return "expected an HTTPError subclass or a zero-argument factory"
    try:
        inspect.signature(on_missing).bind()
    except TypeError:
        return "a factory must be callable with no arguments"
    except ValueError:  # pragma: no cover - C callables without a signature: trust them
        return None
    return None


def state_name(key: Any) -> Any:
    """The storage name of a ``StateKey`` (other keys are returned unchanged)."""
    return key.name if isinstance(key, StateKey) else key


def provides_state(*keys: StateKey[Any]) -> Callable[[_X], _X]:
    """Declare that a guard, middleware or interceptor class sets these state keys.

    Pure metadata (own ``__dict__``; a subclass must re-declare it). Stacking
    accumulates. ``LaurenFactory.create`` uses it to check routes that inject
    a ``StateKey(..., require_provider=True)``::

        @provides_state(CurrentUser)
        class JwtGuard:
            async def can_activate(self, ctx) -> bool:
                ctx.request.state[CurrentUser] = AuthUser(...)
                return True
    """
    if keys and isinstance(keys[0], type):
        reject_bare_usage("provides_state", keys[0])
    if not keys:
        raise DecoratorUsageError(
            "@provides_state needs at least one StateKey: write '@provides_state(CurrentUser)'",
            detail={"decorator": "provides_state", "reason": "no_state_keys"},
        )
    for key in keys:
        if not isinstance(key, StateKey):
            raise DecoratorUsageError(
                f"@provides_state accepts StateKey objects, got {key!r}",
                detail={"decorator": "provides_state", "reason": "not_a_state_key", "value": repr(key)},
            )

    def decorator(cls: _X) -> _X:
        if not isinstance(cls, type):
            raise DecoratorUsageError(
                f"@provides_state decorates a guard, middleware or interceptor class, got {cls!r}",
                detail={"decorator": "provides_state", "target": _target_name(cls), "reason": "not_a_class"},
            )
        existing = tuple(cls.__dict__.get(PROVIDES_STATE_META, ()))
        setattr(cls, PROVIDES_STATE_META, existing + tuple(k for k in keys if k not in existing))
        return cls

    return decorator


def provided_state(component: Any) -> tuple[StateKey[Any], ...]:
    """The keys a component declares with ``@provides_state`` (own ``__dict__`` for classes)."""
    if isinstance(component, type):
        return tuple(component.__dict__.get(PROVIDES_STATE_META, ()))
    return tuple(getattr(component, PROVIDES_STATE_META, ()))


def check_state_provenance(
    extractions: Iterable[Any],
    chain: Iterable[Any],
    *,
    owner: str,
    handler: str,
    route: str,
    candidates: Iterable[Any],
) -> None:
    """Fail startup when a ``require_provider=True`` state key has no provider in *chain*."""
    required = [
        ext
        for ext in extractions
        if getattr(ext, "state_key", None) is not None
        and ext.source == "state_key"
        and ext.state_key.require_provider
        and not ext.has_default
    ]
    if not required:
        return
    provided = {key.name for component in chain for key in provided_state(component)}
    for ext in required:
        key = ext.state_key
        if key.name in provided:
            continue
        providers = sorted(
            {_target_name(c) for c in candidates if any(k.name == key.name for k in provided_state(c))}
        )
        if providers:
            hint = (
                f"{', '.join(providers)} provide{'s' if len(providers) == 1 else ''} it — add "
                f"@use_guards / @use_middlewares / @use_interceptors with one of them to {owner} or to the route"
            )
        else:
            hint = (
                "no registered guard, middleware or interceptor declares it — decorate the class that "
                "sets it with @provides_state(key)"
            )
        raise UnresolvableParameterError(
            f"Parameter {ext.name!r} of {owner}.{handler} ({route}) requires {key!r}, but nothing in "
            f"the route's chain provides it.\n  help: {hint}",
            detail={
                "param": ext.name,
                "handler": f"{owner}.{handler}",
                "route": route,
                "state_key": key.name,
                "providers": providers,
                "reason": "state_not_provided",
            },
        )


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompositionRecord:
    """One ``compose(...)`` application recorded on its target."""

    name: str | None
    decorators: tuple[str, ...]


def describe_decorator(decorator: Any) -> str:
    """A short label for a decorator in provenance records and error messages."""
    if isinstance(decorator, _Composition):
        return decorator.name or repr(decorator)
    if isinstance(decorator, _Decoration):
        return repr(decorator)
    qualname = str(getattr(decorator, "__qualname__", None) or type(decorator).__qualname__)
    # ``use_guards.<locals>.decorator`` → ``use_guards``: closures are named
    # after the factory that built them.
    parts = qualname.split(".<locals>.")
    while len(parts) > 1 and parts[-1] in _CLOSURE_NAMES:
        parts.pop()
    return parts[-1]


_CLOSURE_NAMES = frozenset({"decorator", "deco", "wrapper", "inner", "factory"})


class _Composition:
    """The decorator :func:`compose` returns."""

    __slots__ = ("decorators", "name")

    def __init__(self, decorators: tuple[Callable[[Any], Any], ...], name: str | None) -> None:
        self.decorators = decorators
        self.name = name

    def __call__(self, target: _X) -> _X:
        for decorator in reversed(self.decorators):
            result = decorator(target)
            if result is not target:
                label = describe_decorator(decorator)
                raise DecoratorUsageError(
                    f"{self!r}: {label} returned {result!r} instead of the object it decorated "
                    f"({_target_name(target)}). Composed decorators must attach metadata and return "
                    "their target; apply wrapping decorators outside the composition.",
                    detail={
                        "reason": "decorator_replaced_target",
                        "decorator": label,
                        "target": _target_name(target),
                        "composition": self.name,
                    },
                )
        record = CompositionRecord(self.name, tuple(describe_decorator(d) for d in self.decorators))
        if isinstance(target, type):
            existing = list(target.__dict__.get(COMPOSED_META) or ())
        else:
            existing = list(getattr(target, COMPOSED_META, None) or ())
        existing.append(record)
        _store(target, COMPOSED_META, existing)
        return target

    def __repr__(self) -> str:
        parts = [describe_decorator(d) for d in self.decorators]
        if self.name is not None:
            parts.append(f"name={self.name!r}")
        return f"compose({', '.join(parts)})"


def compose(*decorators: Callable[[Any], Any], name: str | None = None) -> Callable[[_X], _X]:
    """Bundle decorators into one reusable decorator.

    ``compose(a, b)(f)`` is exactly ``a(b(f))`` — the decorators apply as if
    stacked in the written order, so accumulation semantics are unchanged.
    Every step must return the object it received (Lauren decorators only
    attach metadata): a wrapping decorator raises
    :class:`DecoratorUsageError` naming it. Each application is recorded on
    the target (``lauren.reflect.reflect_composed``)::

        def auth(*roles: str):
            reject_bare_usage("auth", roles[0] if roles else None)
            return compose(use_guards(JwtGuard, RolesGuard), Roles(*roles), name="auth")
    """
    if not decorators:
        raise DecoratorUsageError(
            "compose() needs at least one decorator",
            detail={"decorator": "compose", "reason": "empty_composition"},
        )
    for decorator in decorators:
        if not callable(decorator):
            raise DecoratorUsageError(
                f"compose() accepts decorators, got {decorator!r}",
                detail={"decorator": "compose", "reason": "not_a_decorator", "value": repr(decorator)},
            )
    if name is not None and not isinstance(name, str):
        raise DecoratorUsageError(
            f"compose(name=...) must be a string, got {name!r}",
            detail={"decorator": "compose", "reason": "invalid_name"},
        )
    return _Composition(tuple(decorators), name)


def composed_records(target: Any) -> tuple[CompositionRecord, ...]:
    """The ``compose`` records on *target* (own ``__dict__`` for classes)."""
    if isinstance(target, type):
        return tuple(target.__dict__.get(COMPOSED_META) or ())
    return tuple(getattr(target, COMPOSED_META, None) or ())


__all__ = [
    "COMPOSED_META",
    "METADATA_KEYS",
    "PROVIDES_STATE_META",
    "SET_METADATA",
    "CompositionRecord",
    "MetadataKey",
    "MetadataSetKey",
    "StateKey",
    "check_state_provenance",
    "compose",
    "composed_records",
    "merge_metadata_levels",
    "own_metadata",
    "propagate_user_metadata",
    "provided_state",
    "provides_state",
    "read_metadata",
    "reject_bare_usage",
    "stack_metadata",
    "stack_metadata_dicts",
    "state_name",
]
