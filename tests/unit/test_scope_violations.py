"""Scope-narrowing rules for injectable dependencies.

Lauren enforces the classic DI rule that **wider-scoped injectables must
not depend on narrower-scoped ones**. Violating the rule would either
keep a short-lived instance alive past its sensible lifetime (memory /
state leaks) or cause the framework to silently cache a per-request
value on a long-lived object (a correctness bug that's very hard to
debug in production).

The scope lattice, from widest to narrowest:

    SINGLETON  (one instance per application)
        │
    REQUEST    (one instance per request)
        │
    TRANSIENT  (fresh instance on every resolution)

Rules enforced at ``DIContainer.compile()``:

* ``SINGLETON`` may depend on ``SINGLETON`` only.
* ``REQUEST``   may depend on ``SINGLETON`` or ``REQUEST``.
* ``TRANSIENT`` may depend on anything (it's ephemeral by definition, so
  it's safe for it to receive a longer-lived collaborator).

Any narrowing in the other direction must raise
:class:`DIScopeViolationError` at compile time — never at request time,
which is too late and too painful to debug.
"""

from __future__ import annotations

import pytest

from lauren import DIContainer, Scope, injectable
from lauren.exceptions import DIScopeViolationError


# ---------------------------------------------------------------------------
# SINGLETON — the strictest scope.
# ---------------------------------------------------------------------------


class TestSingletonCannotDependOnNarrower:
    """SINGLETON can depend on SINGLETON only."""

    def test_singleton_depending_on_request_raises(self):
        @injectable(scope=Scope.REQUEST)
        class Session:
            pass

        @injectable(scope=Scope.SINGLETON)
        class Repo:
            def __init__(self, s: Session) -> None:
                self._s = s

        c = DIContainer()
        c.register(Session)
        c.register(Repo)
        with pytest.raises(DIScopeViolationError) as excinfo:
            c.compile()
        # The error should name the offending dependent.
        msg = str(excinfo.value)
        assert "Singleton" in msg or "singleton" in msg

    def test_singleton_depending_on_transient_raises(self):
        @injectable(scope=Scope.TRANSIENT)
        class Clock:
            pass

        @injectable(scope=Scope.SINGLETON)
        class Scheduler:
            def __init__(self, clock: Clock) -> None:
                self._clock = clock

        c = DIContainer()
        c.register(Clock)
        c.register(Scheduler)
        with pytest.raises(DIScopeViolationError):
            c.compile()

    def test_singleton_depending_on_singleton_is_ok(self):
        @injectable(scope=Scope.SINGLETON)
        class Settings:
            pass

        @injectable(scope=Scope.SINGLETON)
        class Engine:
            def __init__(self, settings: Settings) -> None:
                self._settings = settings

        c = DIContainer()
        c.register(Settings)
        c.register(Engine)
        # No exception — compiles cleanly.
        c.compile()

    def test_singleton_chain_of_singletons_is_ok(self):
        """A → B → C all SINGLETON is always fine."""

        @injectable(scope=Scope.SINGLETON)
        class C:
            pass

        @injectable(scope=Scope.SINGLETON)
        class B:
            def __init__(self, c: C) -> None:
                self._c = c

        @injectable(scope=Scope.SINGLETON)
        class A:
            def __init__(self, b: B) -> None:
                self._b = b

        c = DIContainer()
        c.register(C)
        c.register(B)
        c.register(A)
        c.compile()


# ---------------------------------------------------------------------------
# REQUEST — may depend on SINGLETON or REQUEST; must not depend on TRANSIENT.
# ---------------------------------------------------------------------------


class TestRequestScope:
    def test_request_depending_on_singleton_is_ok(self):
        @injectable(scope=Scope.SINGLETON)
        class Config:
            pass

        @injectable(scope=Scope.REQUEST)
        class Session:
            def __init__(self, config: Config) -> None:
                self._config = config

        c = DIContainer()
        c.register(Config)
        c.register(Session)
        c.compile()

    def test_request_depending_on_request_is_ok(self):
        @injectable(scope=Scope.REQUEST)
        class SessionScope:
            pass

        @injectable(scope=Scope.REQUEST)
        class AuditLogger:
            def __init__(self, session: SessionScope) -> None:
                self._session = session

        c = DIContainer()
        c.register(SessionScope)
        c.register(AuditLogger)
        c.compile()

    def test_request_depending_on_transient_raises(self):
        """REQUEST-scoped should not keep a TRANSIENT alive across the request.

        TRANSIENT's contract is "new instance every resolution", so
        caching it on a REQUEST-scoped object subtly breaks the contract
        — subsequent resolves from the same request would share the first
        instance rather than receiving fresh ones.
        """

        @injectable(scope=Scope.TRANSIENT)
        class Ticker:
            pass

        @injectable(scope=Scope.REQUEST)
        class Tracker:
            def __init__(self, t: Ticker) -> None:
                self._t = t

        c = DIContainer()
        c.register(Ticker)
        c.register(Tracker)
        with pytest.raises(DIScopeViolationError):
            c.compile()


# ---------------------------------------------------------------------------
# TRANSIENT — may depend on anything.
# ---------------------------------------------------------------------------


class TestTransientCanDependOnAnything:
    def test_transient_depending_on_singleton_is_ok(self):
        @injectable(scope=Scope.SINGLETON)
        class Registry:
            pass

        @injectable(scope=Scope.TRANSIENT)
        class Id:
            def __init__(self, reg: Registry) -> None:
                self._reg = reg

        c = DIContainer()
        c.register(Registry)
        c.register(Id)
        c.compile()

    def test_transient_depending_on_request_is_ok(self):
        @injectable(scope=Scope.REQUEST)
        class Session:
            pass

        @injectable(scope=Scope.TRANSIENT)
        class QueryBuilder:
            def __init__(self, session: Session) -> None:
                self._session = session

        c = DIContainer()
        c.register(Session)
        c.register(QueryBuilder)
        c.compile()

    def test_transient_depending_on_transient_is_ok(self):
        @injectable(scope=Scope.TRANSIENT)
        class Inner:
            pass

        @injectable(scope=Scope.TRANSIENT)
        class Outer:
            def __init__(self, inner: Inner) -> None:
                self._inner = inner

        c = DIContainer()
        c.register(Inner)
        c.register(Outer)
        c.compile()


# ---------------------------------------------------------------------------
# Transitive narrowing — the violation must be caught even when the
# illegal edge is several hops away.
# ---------------------------------------------------------------------------


class TestTransitiveScopeRules:
    def test_singleton_depends_on_singleton_that_depends_on_request_raises(self):
        """A: SINGLETON → B: SINGLETON → C: REQUEST — the A→B edge is fine,
        but the B→C edge is a violation that compile must detect.
        """

        @injectable(scope=Scope.REQUEST)
        class C:
            pass

        @injectable(scope=Scope.SINGLETON)
        class B:
            def __init__(self, c: C) -> None:
                self._c = c

        @injectable(scope=Scope.SINGLETON)
        class A:
            def __init__(self, b: B) -> None:
                self._b = b

        container = DIContainer()
        container.register(C)
        container.register(B)
        container.register(A)
        with pytest.raises(DIScopeViolationError):
            container.compile()


# ---------------------------------------------------------------------------
# The error payload is useful: it names both the offending scope and the
# dependency that caused the violation.
# ---------------------------------------------------------------------------


class TestErrorPayloadIsActionable:
    def test_error_detail_includes_dependent_scope(self):
        @injectable(scope=Scope.REQUEST)
        class Req:
            pass

        @injectable(scope=Scope.SINGLETON)
        class S:
            def __init__(self, r: Req) -> None:
                self._r = r

        c = DIContainer()
        c.register(Req)
        c.register(S)
        with pytest.raises(DIScopeViolationError) as excinfo:
            c.compile()
        detail = getattr(excinfo.value, "detail", {}) or {}
        # The current implementation already records the dependent scope.
        assert detail.get("dependent_scope") == "singleton"
