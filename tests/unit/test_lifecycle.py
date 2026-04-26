"""Unit tests for lifecycle hooks."""

from __future__ import annotations

import asyncio

import pytest

from lauren import injectable, post_construct, pre_destruct
from lauren._di import DIContainer
from lauren._lifecycle import LifecycleScheduler


class TestLifecycleHooks:
    @pytest.mark.asyncio
    async def test_post_construct_runs(self):
        calls: list[str] = []

        @injectable()
        class Svc:
            def __init__(self):
                calls.append("init")

            @post_construct
            async def start(self):
                calls.append("post")

        c = DIContainer()
        c.register(Svc)
        c.compile()
        sched = LifecycleScheduler(c)
        sched.compute_order()
        await sched.run_post_construct()
        assert calls == ["init", "post"]

    @pytest.mark.asyncio
    async def test_topological_order(self):
        order: list[str] = []

        @injectable()
        class A:
            @post_construct
            async def start(self):
                order.append("A")

        @injectable()
        class B:
            def __init__(self, a: A): ...

            @post_construct
            async def start(self):
                order.append("B")

        @injectable()
        class C:
            def __init__(self, b: B): ...

            @post_construct
            async def start(self):
                order.append("C")

        container = DIContainer()
        container.register(A)
        container.register(B)
        container.register(C)
        container.compile()
        sched = LifecycleScheduler(container)
        sched.compute_order()
        await sched.run_post_construct()
        # A must come before B, B before C
        assert order.index("A") < order.index("B") < order.index("C")

    @pytest.mark.asyncio
    async def test_pre_destruct_reverse_order(self):
        order: list[str] = []

        @injectable()
        class A:
            @pre_destruct
            async def stop(self):
                order.append("A")

        @injectable()
        class B:
            def __init__(self, a: A): ...

            @pre_destruct
            async def stop(self):
                order.append("B")

        container = DIContainer()
        container.register(A)
        container.register(B)
        container.compile()
        sched = LifecycleScheduler(container)
        sched.compute_order()
        await sched.run_post_construct()
        errors = await sched.run_pre_destruct()
        assert errors == []
        # B (higher in graph) shutdown first, then A
        assert order == ["B", "A"]

    @pytest.mark.asyncio
    async def test_pre_destruct_timeout(self):
        @injectable()
        class Slow:
            @pre_destruct
            async def stop(self):
                await asyncio.sleep(5)

        container = DIContainer()
        container.register(Slow)
        container.compile()
        sched = LifecycleScheduler(container)
        sched.compute_order()
        await sched.run_post_construct()
        errors = await sched.run_pre_destruct(timeout=0.05)
        assert len(errors) == 1
        from lauren.exceptions import DestructTimeoutError

        assert isinstance(errors[0], DestructTimeoutError)

    @pytest.mark.asyncio
    async def test_pre_destruct_error_captured(self):
        @injectable()
        class Bad:
            @pre_destruct
            async def stop(self):
                raise RuntimeError("boom")

        container = DIContainer()
        container.register(Bad)
        container.compile()
        sched = LifecycleScheduler(container)
        sched.compute_order()
        await sched.run_post_construct()
        errors = await sched.run_pre_destruct()
        assert len(errors) == 1
        from lauren.exceptions import DestructError

        assert isinstance(errors[0], DestructError)
