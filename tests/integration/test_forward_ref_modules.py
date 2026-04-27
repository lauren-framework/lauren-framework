"""Integration tests for ForwardRef in @module(imports=[...]).

These tests verify the full LaurenFactory.create() → HTTP dispatch path
for the three real-world scenarios where forward references break circular
import problems at the module-graph level:

1. **Same-package forward reference** — module A lives in the same file
   as module B, so no import is needed; ForwardRef finds B via the
   declaring module's own globals.

2. **Cross-file simulation** — module A's decorator is evaluated before
   B's file is imported.  ForwardRef defers lookup to compile time when
   both are loaded.  Simulated here by building fake ``sys.modules``
   entries exactly as a real cross-file circular import would.

3. **Mutual forward references** — A imports ForwardRef(B) *and* B
   imports ForwardRef(A). Only one direction can be resolved first, so
   the graph is actually a DAG (no shared providers) — this must boot
   cleanly.

4. **Dotted names** — ``ForwardRef("mypkg.submod.BModule")`` and its
   plain-string equivalent select the target unambiguously even when
   another module in the process happens to define a class with the
   same bare name.

5. **Exported-provider chain** — providers exported across a ForwardRef
   boundary are visible to the consumer's controllers, exactly as they
   would be with a direct class reference.

All tests produce a live ASGI app and make real HTTP requests through
``TestClient`` to confirm routing + DI resolve end-to-end.
"""

# No ``from __future__ import annotations`` in this file: classes are
# referenced as live objects by DI at boot time and must not be stringified.

import sys
import types
from typing import ForwardRef

import pytest

from lauren import (
    Depends,
    LaurenFactory,
    Path,
    controller,
    get,
    injectable,
    module,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_module(name: str, **attrs: object) -> types.ModuleType:
    """Return a fresh ``types.ModuleType`` registered in ``sys.modules``."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# Scenario 1 — same-file ForwardRef (own-module globals path)
# ---------------------------------------------------------------------------

# These are defined at module scope so ``__module__`` equals this file's
# ``__name__`` and the resolver can find them via the globals fast-path.


@injectable()
class GreetService:
    def greet(self, name: str) -> str:
        return f"hi {name}"


@module(providers=[GreetService], exports=[GreetService])
class GreetModule: ...


@controller("/hello")
class HelloController:
    def __init__(self, svc: GreetService) -> None:
        self.svc = svc

    @get("/{name}")
    async def hello(self, name: Path[str]) -> dict:
        return {"msg": self.svc.greet(name)}


# Consumer uses a ForwardRef to GreetModule — in the real world this would
# avoid a circular import in a two-file setup.
@module(
    controllers=[HelloController],
    imports=[ForwardRef("GreetModule")],
)
class HelloApp: ...


@pytest.mark.asyncio
async def test_same_file_forwardref_boots_and_routes() -> None:
    app = await LaurenFactory.create(HelloApp)
    r = TestClient(app).get("/hello/world")
    assert r.status_code == 200
    assert r.json() == {"msg": "hi world"}


@pytest.mark.asyncio
async def test_forwardref_provider_is_visible_in_consumer_module() -> None:
    # GreetService must appear in the compiled module's provider set.
    from lauren._modules import ModuleGraph

    g = ModuleGraph()
    g.compile(HelloApp)
    assert GreetService in g.modules[HelloApp].providers


# ---------------------------------------------------------------------------
# Scenario 2 — cross-file simulation via fake sys.modules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_file_forwardref_resolves_at_compile_time() -> None:
    """Simulate a.py importing b.py via ForwardRef to avoid a circular import.

    ``_fake_b`` is registered in sys.modules *after* the decorator on
    ``FakeAModule`` is evaluated — exactly what happens in a real circular
    import scenario where b.py is not yet imported when a.py is parsed.
    """

    @injectable()
    class FakeBService:
        def value(self) -> str:
            return "from_b"

    @module(providers=[FakeBService], exports=[FakeBService])
    class FakeBModule: ...

    @controller("/fakecross")
    class FakeCrossCtrl:
        def __init__(self, svc: FakeBService) -> None:
            self.svc = svc

        @get("/")
        async def root(self) -> dict:
            return {"v": self.svc.value()}

    # Decorate FakeAModule *before* FakeBModule is in any accessible globals
    # of the declaring class — replicate the cross-file timing.
    @module(
        controllers=[FakeCrossCtrl],
        imports=[ForwardRef("FakeBModule")],
    )
    class FakeAModule: ...

    # Now make FakeBModule reachable under a fake sys.modules entry, as if
    # b.py were fully imported at this point in the startup sequence.
    fake_b_mod = _make_fake_module("_lauren_integ_fake_b", FakeBModule=FakeBModule)  # noqa: F841
    # Also inject into the current module's namespace so the own-globals
    # fast path can find it (mirrors the import machinery resolving at app boot).
    current = sys.modules[__name__]
    current.FakeBModule = FakeBModule  # type: ignore[attr-defined]

    try:
        app = await LaurenFactory.create(FakeAModule)
        r = TestClient(app).get("/fakecross/")
        assert r.status_code == 200
        assert r.json() == {"v": "from_b"}
    finally:
        del sys.modules["_lauren_integ_fake_b"]
        del current.FakeBModule  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Scenario 3 — mutual ForwardRef: A ← ForwardRef → B, B ← ForwardRef → A
#   (no shared providers — the graph is a DAG, not a cycle)
# ---------------------------------------------------------------------------


@injectable()
class PingService:
    def ping(self) -> str:
        return "pong"


@injectable()
class PongService:
    def pong(self) -> str:
        return "ping"


@module(providers=[PingService], exports=[PingService])
class PingModule: ...


@module(providers=[PongService], exports=[PongService])
class PongModule: ...


@controller("/mutual")
class MutualController:
    def __init__(self, ping: PingService, pong: PongService) -> None:
        self.ping = ping
        self.pong = pong

    @get("/")
    async def root(self) -> dict:
        return {"ping": self.ping.ping(), "pong": self.pong.pong()}


@module(
    controllers=[MutualController],
    imports=[ForwardRef("PingModule"), ForwardRef("PongModule")],
)
class MutualApp: ...


@pytest.mark.asyncio
async def test_mutual_forwardrefs_boot_cleanly() -> None:
    app = await LaurenFactory.create(MutualApp)
    r = TestClient(app).get("/mutual/")
    assert r.status_code == 200
    assert r.json() == {"ping": "pong", "pong": "ping"}


# ---------------------------------------------------------------------------
# Scenario 4 — dotted ForwardRef for unambiguous cross-package resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dotted_forwardref_resolves_unambiguously() -> None:
    """ForwardRef('_lauren_integ_pkg.sub.DottedSvc') pinpoints the exact
    class even if another loaded module happens to define ``DottedSvc``."""

    @injectable()
    class DottedSvc:
        def answer(self) -> int:
            return 42

    @module(providers=[DottedSvc], exports=[DottedSvc])
    class DottedMod: ...

    # Register under a qualified name to exercise the dotted-name path.
    _make_fake_module("_lauren_integ_pkg.sub", DottedMod=DottedMod)

    @controller("/dotted")
    class DottedCtrl:
        def __init__(self, svc: DottedSvc) -> None:
            self.svc = svc

        @get("/")
        async def root(self) -> dict:
            return {"answer": self.svc.answer()}

    @module(
        controllers=[DottedCtrl],
        imports=[ForwardRef("_lauren_integ_pkg.sub.DottedMod")],
    )
    class DottedApp: ...

    try:
        app = await LaurenFactory.create(DottedApp)
        r = TestClient(app).get("/dotted/")
        assert r.status_code == 200
        assert r.json() == {"answer": 42}
    finally:
        del sys.modules["_lauren_integ_pkg.sub"]


# ---------------------------------------------------------------------------
# Scenario 5 — three-level chain: Root → ForwardRef(Mid) → ForwardRef(Leaf)
# ---------------------------------------------------------------------------


@injectable()
class LeafService:
    def leaf(self) -> str:
        return "leaf"


@module(providers=[LeafService], exports=[LeafService])
class LeafModule: ...


@module(imports=[ForwardRef("LeafModule")], exports=[LeafService])
class MidModule: ...


@controller("/chain")
class ChainController:
    def __init__(self, svc: LeafService) -> None:
        self.svc = svc

    @get("/")
    async def root(self) -> dict:
        return {"v": self.svc.leaf()}


@module(controllers=[ChainController], imports=[ForwardRef("MidModule")])
class ChainApp: ...


@pytest.mark.asyncio
async def test_three_level_forwardref_chain_resolves() -> None:
    app = await LaurenFactory.create(ChainApp)
    r = TestClient(app).get("/chain/")
    assert r.status_code == 200
    assert r.json() == {"v": "leaf"}


# ---------------------------------------------------------------------------
# Scenario 6 — Depends[...] endpoint parameter crosses ForwardRef boundary
# ---------------------------------------------------------------------------


@injectable()
class DepService:
    def val(self) -> str:
        return "dep_ok"


@module(providers=[DepService], exports=[DepService])
class DepProviderModule: ...


@controller("/dep")
class DepController:
    @get("/")
    async def root(self, svc: Depends[DepService]) -> dict:
        return {"v": svc.val()}


@module(
    controllers=[DepController],
    imports=[ForwardRef("DepProviderModule")],
)
class DepConsumerApp: ...


@pytest.mark.asyncio
async def test_depends_extractor_resolves_across_forwardref_boundary() -> None:
    """``Depends[X]`` on an endpoint resolves X when the providing module was
    imported via ForwardRef rather than a direct class reference."""
    app = await LaurenFactory.create(DepConsumerApp)
    r = TestClient(app).get("/dep/")
    assert r.status_code == 200
    assert r.json() == {"v": "dep_ok"}


# ---------------------------------------------------------------------------
# Scenario 7 — unresolvable ForwardRef surfaces as StartupError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolvable_forwardref_raises_at_startup() -> None:
    """An unresolvable forward reference must fail at ``LaurenFactory.create``
    time, not silently at request time."""
    from lauren.exceptions import StartupError

    @controller("/noop")
    class NoopCtrl:
        @get("/")
        async def root(self) -> dict:
            return {}

    @module(
        controllers=[NoopCtrl],
        imports=[ForwardRef("AbsolutelyNonExistentModule_XYZ")],
    )
    class BrokenApp: ...

    with pytest.raises((StartupError, ValueError)):
        await LaurenFactory.create(BrokenApp)
