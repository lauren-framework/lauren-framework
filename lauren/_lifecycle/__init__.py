"""Lifecycle scheduler — runs ``@post_construct`` / ``@pre_destruct`` hooks."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass

from .._di import DIContainer, Provider
from ..exceptions import (
    CircularDependencyError,
    DestructError,
    DestructTimeoutError,
)


@dataclass
class _Node:
    provider: Provider
    deps: list[Provider]


class LifecycleScheduler:
    """Computes topological order and invokes lifecycle hooks."""

    def __init__(self, container: DIContainer) -> None:
        self._container = container
        self._order: list[Provider] = []

    def compute_order(self) -> list[Provider]:
        """Return providers in topological order (deps first)."""
        providers = self._container.all_providers()
        # Build map class -> provider
        by_cls = {p.cls: p for p in providers}

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[type, int] = {}
        order: list[Provider] = []
        path: list[type] = []

        def visit(p: Provider) -> None:
            c = color.get(p.cls, WHITE)
            if c == GRAY:
                cycle = [x.__name__ for x in path[path.index(p.cls) :] + [p.cls]]
                raise CircularDependencyError(
                    "Circular lifecycle dependency: " + " -> ".join(cycle),
                    detail={"cycle": cycle},
                )
            if c == BLACK:
                return
            color[p.cls] = GRAY
            path.append(p.cls)
            for _, dep_type in p.deps:
                dep = by_cls.get(dep_type)
                if dep:
                    visit(dep)
                else:
                    # May be a protocol / token. Resolve if possible.
                    try:
                        prov = self._container.get_provider(dep_type)
                        visit(prov)
                    except Exception:
                        continue
            path.pop()
            color[p.cls] = BLACK
            order.append(p)

        for p in providers:
            visit(p)
        self._order = order
        return order

    async def run_post_construct(self) -> None:
        """Instantiate singleton providers and invoke ``@post_construct`` hooks.

        Singleton hooks are invoked in topological (deps-first) order. The
        container is told explicitly that the hook has fired so that a later
        ad-hoc ``resolve()`` (e.g. from a middleware that eagerly fetches a
        service) does not re-invoke it.

        Request- and transient-scoped providers are not touched here; their
        hooks fire inside :meth:`DIContainer._instantiate` each time an
        instance is constructed during request handling.
        """
        if not self._order:
            self.compute_order()
        from ..types import Scope

        for provider in self._order:
            if provider.scope != Scope.SINGLETON:
                continue
            instance = await self._container.resolve(provider.cls)
            hook = provider.post_construct
            if hook is not None:
                bound = getattr(instance, hook.__name__)
                result = bound()
                if inspect.isawaitable(result):
                    await result
            # Tell the container we've run the hook — prevents double-fire if
            # anyone resolves this singleton post-startup.
            self._container.mark_singleton_initialized(provider.cls)

    async def run_pre_destruct(self, *, timeout: float = 10.0) -> list[Exception]:
        """Invoke ``@pre_destruct`` hooks in reverse topological order.

        Returns a list of exceptions raised by hooks (best-effort shutdown).
        """
        errors: list[Exception] = []
        singletons = self._container.singletons()
        for provider in reversed(self._order):
            hook = provider.pre_destruct
            if hook is None:
                continue
            instance = singletons.get(provider.cls)
            if instance is None:
                continue
            try:
                bound = getattr(instance, hook.__name__)
                result = bound()
                if inspect.isawaitable(result):
                    try:
                        await asyncio.wait_for(result, timeout=timeout)
                    except asyncio.TimeoutError:
                        err = DestructTimeoutError(
                            f"pre_destruct on {provider.cls.__name__} timed out after {timeout}s",
                            detail={"class": provider.cls.__name__, "timeout": timeout},
                        )
                        errors.append(err)
                        continue
            except Exception as e:
                errors.append(
                    DestructError(
                        f"pre_destruct on {provider.cls.__name__} failed: {e}",
                        detail={"class": provider.cls.__name__, "cause": str(e)},
                    )
                )
        return errors


__all__ = ["LifecycleScheduler"]
