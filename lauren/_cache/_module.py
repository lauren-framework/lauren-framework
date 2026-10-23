"""``CacheModule`` and the factory hooks that wire caching into an app.

* :func:`install_cache` (Phase 2) registers the app's :class:`Cache` when a
  ``CacheModule.for_root(...)`` module is in the graph.
* :func:`compile_cache_routes` (Phase 5) finds ``@cache_response`` /
  ``@idempotent`` handlers, fails startup when no ``Cache`` is available
  (``reason="cache_module_missing"``) and appends the framework interceptor
  as each such route's innermost interceptor. Routes without a policy are
  untouched, so apps that never use caching pay nothing per request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .._di import _structural_protocol_providers
from .._di.custom import use_factory
from .._stores._lifecycle import adopt_store_lifecycle
from .._stores._memory import DEFAULT_MAX_ENTRIES, MemoryStore
from .._stores._module import StoreToken
from .._stores._protocol import Store, check_prefix, check_ttl
from ..decorators import module
from ..exceptions import DecoratorUsageError, StartupError
from ..logging import Logger
from ._codec import CacheCodec, JsonCacheCodec
from ._idempotency import IdempotencyInterceptor
from ._policy import CACHE_RESPONSE_META, IDEMPOTENT_META, CachePolicy, IdempotencyPolicy
from ._response import ResponseCacheInterceptor
from ._service import Cache

#: Attribute carrying a :class:`_CacheSpec` on a generated module class.
CACHE_SPEC_ATTR = "__lauren_cache_spec__"


@dataclass(frozen=True)
class _CacheSpec:
    store: Any
    namespace: str | None
    default_ttl: float | None
    codec: CacheCodec | None
    lock_ttl: float
    lock_wait: float
    clock: Callable[[], float] | None


def _invalid(message: str, **detail: Any) -> DecoratorUsageError:
    return DecoratorUsageError(
        f"CacheModule.for_root(): {message}", detail={"helper": "CacheModule.for_root", **detail}
    )


class CacheModule:
    """Provide the app's :class:`Cache` (import the module ``for_root`` returns)."""

    @classmethod
    def for_root(
        cls,
        *,
        store: Store | str | Callable[[], Store] | None = None,
        namespace: str | None = "cache",
        default_ttl: float | None = 300.0,
        codec: CacheCodec | None = None,
        lock_ttl: float = 30.0,
        lock_wait: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> type:
        """Return a module that registers a :class:`Cache` globally.

        ``store=None`` uses the default ``Store`` provider (``StoresModule``)
        when one is registered, else a private per-app
        ``MemoryStore(max_entries=100_000)``; a string names a
        ``StoresModule`` store; a :class:`Store` instance or zero-argument
        factory is used directly (its lifecycle hooks run with the app).
        Entries live under ``"<namespace>:"``. ``codec=None`` encodes with the
        app's JSON encoder. ``clock`` (wall-clock seconds) is for tests.
        """
        if not (store is None or isinstance(store, (str, Store)) or callable(store)):
            raise _invalid(
                f"store must be None, a store name, a Store or a factory; got {type(store).__name__}",
                reason="invalid_store",
            )
        if isinstance(store, str) and not store:
            raise _invalid("a store name must be non-empty", reason="invalid_store")
        if namespace is not None:
            check_prefix(namespace)
        try:
            check_ttl(default_ttl)
            check_ttl(lock_ttl, allow_none=False)
        except (TypeError, ValueError) as exc:
            raise _invalid(str(exc), reason="invalid_ttl") from None
        if isinstance(lock_wait, bool) or not isinstance(lock_wait, (int, float)) or lock_wait < 0:
            raise _invalid(
                f"lock_wait must be a number of seconds >= 0; got {lock_wait!r}", reason="invalid_ttl"
            )
        if codec is not None and not isinstance(codec, CacheCodec):
            raise _invalid(
                f"codec must implement encode()/decode(); got {type(codec).__name__}", reason="invalid_codec"
            )
        spec = _CacheSpec(store, namespace, default_ttl, codec, lock_ttl, lock_wait, clock)
        generated: type = module()(
            type(
                "CacheModule[root]",
                (),
                {
                    "__doc__": "Generated CacheModule (the app's Cache).",
                    "__module__": __name__,
                    CACHE_SPEC_ATTR: spec,
                },
            )
        )
        return generated


def _has_store_provider(container: Any) -> bool:
    if container.has_provider(Store):
        return True
    return bool(_structural_protocol_providers(Store, None, container._providers))


def install_cache(graph: Any, container: Any, *, encoder: Any) -> None:
    """Phase 2: register the :class:`Cache` described by ``CacheModule.for_root``."""
    specs: list[_CacheSpec] = []
    for mod in graph.modules:
        spec = mod.__dict__.get(CACHE_SPEC_ATTR)
        if spec is not None and spec not in specs:
            specs.append(spec)
    if not specs:
        return
    if len(specs) > 1:
        raise StartupError(
            "CacheModule.for_root() is imported with different options; configure the cache once",
            detail={"reason": "conflicting_cache_options", "count": len(specs)},
        )
    if container.has_provider(Cache):
        return  # a Cache binding the application registered itself wins
    spec = specs[0]
    inject: tuple[Any, ...] = ()
    fixed: Store | None = None
    if spec.store is None:
        if _has_store_provider(container):
            inject = (Store,)
        else:
            fixed = MemoryStore(max_entries=DEFAULT_MAX_ENTRIES)
    elif isinstance(spec.store, str):
        token = StoreToken(spec.store)
        if not container.has_provider(token):
            raise StartupError(
                f"CacheModule.for_root(store={spec.store!r}) names a store that StoresModule does not register",
                detail={"reason": "invalid_store", "name": spec.store},
            )
        inject = (token,)
    elif not isinstance(spec.store, type) and isinstance(spec.store, Store):
        fixed = spec.store
    else:
        made = spec.store()
        if isinstance(made, type) or not isinstance(made, Store):
            raise StartupError(
                f"the CacheModule store factory returned {type(made).__name__}, not a Store",
                detail={"reason": "invalid_store", "got": type(made).__name__},
            )
        fixed = made
    if fixed is not None:
        adopt_store_lifecycle(container, fixed)
    codec = spec.codec if spec.codec is not None else JsonCacheCodec(encoder)

    def _make_cache(store: Store | None = None) -> Cache:
        backend = fixed if store is None else store
        assert backend is not None
        return Cache(
            backend.namespace(spec.namespace) if spec.namespace else backend,
            default_ttl=spec.default_ttl,
            codec=codec,
            lock_ttl=spec.lock_ttl,
            lock_wait=spec.lock_wait,
            clock=spec.clock,
        )

    provider = container.register_custom(
        use_factory(provide=Cache, factory=_make_cache, injects=inject), owning_module=None
    )
    if provider.factory is _make_cache:
        # Cancel stale-while-revalidate refreshes at shutdown.
        provider.pre_destruct = Cache.aclose


def _handler_name(ch: Any) -> str:
    return f"{ch.controller_cls.__name__}.{ch.handler_fn.__name__}"


def compile_cache_routes(
    compiled_handlers: dict[tuple[str, str], Any], container: Any, *, logger: Logger
) -> None:
    """Phase 5: validate cache / idempotency policies and attach their interceptors."""
    cached: dict[Callable[..., Any], CachePolicy] = {}
    idempotent: dict[Callable[..., Any], IdempotencyPolicy] = {}
    shared_unkeyed: list[str] = []
    for (method, path), ch in compiled_handlers.items():
        policy: CachePolicy | None = getattr(ch.handler_fn, CACHE_RESPONSE_META, None)
        idem: IdempotencyPolicy | None = getattr(ch.handler_fn, IDEMPOTENT_META, None)
        if policy is None and idem is None:
            continue
        if not container.has_provider(Cache):
            decorator = "@cache_response" if policy is not None else "@idempotent"
            raise StartupError(
                f"handler {_handler_name(ch)} ({method} {path}) uses {decorator} but no Cache is "
                "provided; import CacheModule.for_root() into the app",
                detail={
                    "reason": "cache_module_missing",
                    "handler": _handler_name(ch),
                    "method": method,
                    "path": path,
                },
            )
        if policy is not None:
            cached[ch.handler_fn] = policy
            ch.interceptors = (*ch.interceptors, ResponseCacheInterceptor)
            reads_session = any(ext.source == "session" for ext in ch.extractions)
            if reads_session and not policy.per_caller:
                shared_unkeyed.append(f"{method} {path}")
        if idem is not None:
            idempotent[ch.handler_fn] = idem
            ch.interceptors = (*ch.interceptors, IdempotencyInterceptor)
    if cached:
        container.register_custom(
            use_factory(
                provide=ResponseCacheInterceptor,
                factory=lambda cache: ResponseCacheInterceptor(cache, cached),
                injects=(Cache,),
            ),
            owning_module=None,
        )
        logger.verbose(f"Response caching enabled on {len(cached)} handler(s)", context="CacheModule")
    if idempotent:
        container.register_custom(
            use_factory(
                provide=IdempotencyInterceptor,
                factory=lambda cache: IdempotencyInterceptor(cache, idempotent),
                injects=(Cache,),
            ),
            owning_module=None,
        )
        logger.verbose(f"Idempotency keys enabled on {len(idempotent)} handler(s)", context="CacheModule")
    if shared_unkeyed:
        message = (
            "@cache_response routes read the session but have no key= "
            f"({', '.join(sorted(shared_unkeyed)[:3])}); every user is served the same cached "
            "response — pass key=lambda ctx: ... for per-user data"
        )
        logging.getLogger("lauren").warning(message)
        logger.warn(message, context="CacheModule", routes=sorted(shared_unkeyed))


__all__ = ["CACHE_SPEC_ATTR", "CacheModule", "compile_cache_routes", "install_cache"]
