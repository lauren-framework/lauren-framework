"""``StoresModule`` — register the app's stores as lifecycle-managed singletons.

``StoresModule.for_root(default=..., named={...})`` returns a generated
``@module()`` class carrying a spec. ``LaurenFactory.create`` reads every
spec in the module graph (Phase 2, before sessions) and registers, globally:

* the default store under the :class:`~lauren.stores.Store` token,
* each named store under ``StoreToken(name)``,
* a :class:`StoreRegistry` for dynamic lookups.

Each store is registered as a singleton whose ``startup()`` / ``aclose()``
(or ``@post_construct`` / ``@pre_destruct``) hooks run with the app — unlike
``use_value``, which never runs hooks. A zero-argument factory (for example
the ``MemoryStore`` class bound with ``functools.partial``) is called once per
app, so several apps built from one module never share a store.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from .._di.custom import Token, use_value
from ..decorators import module
from ..exceptions import DecoratorUsageError, StartupError
from ._lifecycle import adopt_store_lifecycle
from ._memory import DEFAULT_MAX_ENTRIES, MemoryStore
from ._protocol import Store

#: Attribute carrying a :class:`_StoresSpec` on a generated module class.
STORES_SPEC_ATTR = "__lauren_stores_spec__"
#: Registry name of the default store.
DEFAULT_STORE_NAME = "default"

StoreSource = Store | Callable[[], Store] | None


class StoreToken(Token):
    """DI token of a named store: ``Annotated[Store, Inject(StoreToken("local"))]``.

    Two ``StoreToken`` objects with the same name are the same token.
    """

    __slots__ = ("_store_name",)

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError(f"StoreToken(name) requires a non-empty string; got {name!r}")
        super().__init__(f"lauren.store:{name}", unique=False)
        self._store_name = name

    @property
    def store_name(self) -> str:
        return self._store_name

    def __repr__(self) -> str:
        return f"StoreToken({self._store_name!r})"


class StoreRegistry:
    """Name-to-store lookup for the stores ``StoresModule`` registered.

    The default store is available as ``registry.get()`` (name ``"default"``).
    """

    __slots__ = ("_stores",)

    def __init__(self, stores: Mapping[str, Store]) -> None:
        self._stores: dict[str, Store] = dict(stores)

    def get(self, name: str = DEFAULT_STORE_NAME) -> Store:
        """Return the store registered as ``name`` (``LookupError`` when unknown)."""
        try:
            return self._stores[name]
        except KeyError:
            known = ", ".join(sorted(self._stores)) or "none"
            raise LookupError(f"no store named {name!r} is registered (registered: {known})") from None

    def names(self) -> tuple[str, ...]:
        return tuple(self._stores)

    def __contains__(self, name: object) -> bool:
        return name in self._stores

    def __iter__(self) -> Iterator[str]:
        return iter(self._stores)

    def __len__(self) -> int:
        return len(self._stores)

    def __repr__(self) -> str:
        return f"StoreRegistry({sorted(self._stores)!r})"


@dataclass(frozen=True)
class _StoresSpec:
    default: StoreSource
    named: tuple[tuple[str, StoreSource], ...]


def _check_source(value: Any, *, name: str) -> None:
    if value is None or callable(value) or isinstance(value, Store):
        return
    raise DecoratorUsageError(
        f"StoresModule.for_root(): store {name!r} must be a Store instance or a zero-argument "
        f"factory returning one; got {type(value).__name__}",
        detail={"reason": "invalid_store", "name": name, "got": type(value).__name__},
    )


def _is_instance(value: Any) -> bool:
    return not isinstance(value, type) and isinstance(value, Store)


class StoresModule:
    """Register the app's key-value stores (import the module ``for_root`` returns)."""

    @classmethod
    def for_root(
        cls,
        *,
        default: StoreSource = None,
        named: Mapping[str, StoreSource] | None = None,
    ) -> type:
        """Return a module that provides ``default`` as ``Store`` and each named store.

        Values are :class:`Store` instances or zero-argument factories called
        once per app. ``default=None`` creates a per-app
        ``MemoryStore(max_entries=100_000)``.
        """
        _check_source(default, name=DEFAULT_STORE_NAME)
        entries: list[tuple[str, StoreSource]] = []
        for name, value in dict(named or {}).items():
            if not isinstance(name, str) or not name:
                raise DecoratorUsageError(
                    f"StoresModule.for_root(): store names must be non-empty strings; got {name!r}",
                    detail={"reason": "invalid_store", "name": repr(name)},
                )
            if name == DEFAULT_STORE_NAME:
                raise DecoratorUsageError(
                    'StoresModule.for_root(): the name "default" is reserved for default=',
                    detail={"reason": "duplicate_store_name", "name": name},
                )
            if value is None:
                raise DecoratorUsageError(
                    f"StoresModule.for_root(): named store {name!r} is None",
                    detail={"reason": "invalid_store", "name": name, "got": "NoneType"},
                )
            _check_source(value, name=name)
            entries.append((name, value))
        namespace = {
            "__doc__": "Generated StoresModule (the app's key-value stores).",
            "__module__": __name__,
            STORES_SPEC_ATTR: _StoresSpec(default, tuple(entries)),
        }
        generated: type = module()(type("StoresModule[root]", (), namespace))
        return generated


def _materialize(source: StoreSource, *, name: str) -> Store:
    if source is None:
        return MemoryStore(max_entries=DEFAULT_MAX_ENTRIES)
    if _is_instance(source):
        return source  # type: ignore[return-value]
    store = source()  # type: ignore[operator]
    if isinstance(store, type) or not isinstance(store, Store):
        raise StartupError(
            f"the factory for store {name!r} returned {type(store).__name__}, which does not "
            "implement the Store protocol",
            detail={"reason": "invalid_store", "name": name, "got": type(store).__name__},
        )
    return store


def _duplicate(name: str, message: str) -> StartupError:
    return StartupError(message, detail={"reason": "duplicate_store_name", "name": name})


def install_stores(graph: Any, container: Any) -> StoreRegistry | None:
    """Phase 2: register every store declared through ``StoresModule.for_root``."""
    specs: list[_StoresSpec] = []
    for mod in graph.modules:
        spec = mod.__dict__.get(STORES_SPEC_ATTR)
        if spec is not None and spec not in specs:
            specs.append(spec)
    if not specs:
        return None
    explicit_defaults = [spec.default for spec in specs if spec.default is not None]
    if len(explicit_defaults) > 1:
        raise _duplicate(
            DEFAULT_STORE_NAME,
            "StoresModule.for_root(default=...) is declared more than once; configure the default store once",
        )
    sources: dict[str, StoreSource] = {
        DEFAULT_STORE_NAME: explicit_defaults[0] if explicit_defaults else None
    }
    for spec in specs:
        for name, value in spec.named:
            if name in sources:
                raise _duplicate(name, f"the store name {name!r} is registered by more than one StoresModule")
            sources[name] = value
    if container.has_provider(Store):
        raise _duplicate(
            DEFAULT_STORE_NAME,
            "a Store provider is already registered; pass it as StoresModule.for_root(default=...) instead",
        )
    stores = {name: _materialize(source, name=name) for name, source in sources.items()}
    for name, store in stores.items():
        token: Any = Store if name == DEFAULT_STORE_NAME else StoreToken(name)
        container.register_instance(token, store)
        adopt_store_lifecycle(container, store)  # a namespaced view's backend too
    registry = StoreRegistry(stores)
    if not container.has_provider(StoreRegistry):
        container.register_custom(use_value(provide=StoreRegistry, value=registry), owning_module=None)
    return registry


__all__ = [
    "DEFAULT_STORE_NAME",
    "STORES_SPEC_ATTR",
    "StoreRegistry",
    "StoreToken",
    "StoresModule",
    "install_stores",
]
