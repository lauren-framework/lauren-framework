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

    # ------------------------------------------------------------------
    # Sync @pre_destruct hooks — should run in thread + respect timeout
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_pre_destruct_runs(self):
        """A sync @pre_destruct hook executes and its result is observed."""
        called: list[bool] = []

        @injectable()
        class SyncSvc:
            @pre_destruct
            def stop(self):
                called.append(True)

        container = DIContainer()
        container.register(SyncSvc)
        container.compile()
        sched = LifecycleScheduler(container)
        sched.compute_order()
        await sched.run_post_construct()
        errors = await sched.run_pre_destruct()
        assert errors == []
        assert called == [True]

    @pytest.mark.asyncio
    async def test_sync_pre_destruct_timeout(self):
        """A sync @pre_destruct hook that blocks is interrupted by the timeout.

        Before the fix this test would block the event loop for the full
        sleep duration; after the fix the sync hook runs in a thread and
        asyncio.wait_for can cancel it.
        """
        import time

        from lauren.exceptions import DestructTimeoutError

        @injectable()
        class BlockingSvc:
            @pre_destruct
            def stop(self):
                time.sleep(0.5)  # intentionally blocking (shortened for test speed)

        container = DIContainer()
        container.register(BlockingSvc)
        container.compile()
        sched = LifecycleScheduler(container)
        sched.compute_order()
        await sched.run_post_construct()
        # With the fix this completes in ~0.05 s; without it the test hangs for 5 s.
        errors = await sched.run_pre_destruct(timeout=0.05)
        assert len(errors) == 1
        assert isinstance(errors[0], DestructTimeoutError)

    @pytest.mark.asyncio
    async def test_sync_pre_destruct_error_captured(self):
        """A sync @pre_destruct hook that raises is captured, not re-raised."""
        from lauren.exceptions import DestructError

        @injectable()
        class ErrSvc:
            @pre_destruct
            def stop(self):
                raise ValueError("sync boom")

        container = DIContainer()
        container.register(ErrSvc)
        container.compile()
        sched = LifecycleScheduler(container)
        sched.compute_order()
        await sched.run_post_construct()
        errors = await sched.run_pre_destruct()
        assert len(errors) == 1
        assert isinstance(errors[0], DestructError)

    @pytest.mark.asyncio
    async def test_sync_pre_destruct_does_not_block_event_loop(self):
        """The event loop stays responsive while a sync hook runs in a thread."""
        import time

        ticks: list[float] = []

        async def ticker():
            for _ in range(3):
                ticks.append(asyncio.get_event_loop().time())
                await asyncio.sleep(0.02)

        @injectable()
        class SlowSync:
            @pre_destruct
            def stop(self):
                time.sleep(0.1)  # 100ms blocking — should not stall the loop

        container = DIContainer()
        container.register(SlowSync)
        container.compile()
        sched = LifecycleScheduler(container)
        sched.compute_order()
        await sched.run_post_construct()

        # Run ticker and pre_destruct concurrently.
        await asyncio.gather(
            sched.run_pre_destruct(timeout=1.0),
            ticker(),
        )
        # If the event loop was blocked, the ticker could not tick.
        assert len(ticks) == 3

    @pytest.mark.asyncio
    async def test_mixed_sync_async_pre_destruct_order(self):
        """Both sync and async @pre_destruct hooks fire in reverse topo order."""
        order: list[str] = []

        @injectable()
        class Base:
            @pre_destruct
            def stop(self):  # sync
                order.append("Base")

        @injectable()
        class Top:
            def __init__(self, b: Base): ...

            @pre_destruct
            async def stop(self):  # async
                order.append("Top")

        container = DIContainer()
        container.register(Base)
        container.register(Top)
        container.compile()
        sched = LifecycleScheduler(container)
        sched.compute_order()
        await sched.run_post_construct()
        errors = await sched.run_pre_destruct()
        assert errors == []
        assert order == ["Top", "Base"]
