"""Unit tests for the :class:`lauren.Scope` enum contract.

Scope is modeled as an :class:`enum.IntEnum` so the scope-narrowing rule
in the DI compiler can be expressed as a single comparison instead of a
bespoke lookup table:

.. code-block:: python

    if consumer_scope > dependency_scope:
        raise DIScopeViolationError(...)

This test module pins down the invariants that justify that refactor:
numeric ordering, stable label strings, structural identity checks (the
enum still plays well with ``isinstance`` and set membership), and the
backward-compatible shape of error details.
"""

from __future__ import annotations

import enum

import pytest

from lauren import DIContainer, Scope, injectable
from lauren.exceptions import DIScopeViolationError


# ---------------------------------------------------------------------------
# Identity & integer semantics.
# ---------------------------------------------------------------------------


class TestScopeIdentity:
    """Scope must remain an ``IntEnum`` subclass with stable members."""

    def test_is_intenum(self):
        assert issubclass(Scope, enum.IntEnum)
        assert isinstance(Scope.SINGLETON, Scope)
        assert isinstance(Scope.SINGLETON, int)

    def test_has_exactly_three_members(self):
        members = {m.name for m in Scope}
        assert members == {"SINGLETON", "REQUEST", "TRANSIENT"}

    def test_numeric_ordering_is_narrow_to_wide(self):
        """TRANSIENT (0) < REQUEST (1) < SINGLETON (2). The order is
        intentional: higher value = wider lifetime, and the dep-graph
        compiler flags ``consumer > dependency`` as a violation."""
        assert int(Scope.TRANSIENT) == 0
        assert int(Scope.REQUEST) == 1
        assert int(Scope.SINGLETON) == 2

    def test_sorted_order_matches_narrow_to_wide(self):
        assert sorted(Scope) == [Scope.TRANSIENT, Scope.REQUEST, Scope.SINGLETON]


# ---------------------------------------------------------------------------
# Comparison operators drive the violation detector.
# ---------------------------------------------------------------------------


class TestScopeComparison:
    """``>`` is how the DI compiler detects scope narrowing."""

    @pytest.mark.parametrize(
        "consumer,dependency,violates",
        [
            (Scope.SINGLETON, Scope.SINGLETON, False),
            (Scope.SINGLETON, Scope.REQUEST, True),
            (Scope.SINGLETON, Scope.TRANSIENT, True),
            (Scope.REQUEST, Scope.SINGLETON, False),
            (Scope.REQUEST, Scope.REQUEST, False),
            (Scope.REQUEST, Scope.TRANSIENT, True),
            (Scope.TRANSIENT, Scope.SINGLETON, False),
            (Scope.TRANSIENT, Scope.REQUEST, False),
            (Scope.TRANSIENT, Scope.TRANSIENT, False),
        ],
    )
    def test_gt_is_the_violation_predicate(self, consumer, dependency, violates):
        """The rule ``consumer > dependency`` must agree with the DI
        compiler's judgement for every pair of scopes.

        Keeping the predicate this explicit guards against future
        refactors silently inverting the ordering (e.g. by making
        ``SINGLETON = 0``): every pair is enumerated and its expected
        verdict asserted against the comparison operator."""
        assert (consumer > dependency) is violates

    def test_lt_is_the_inverse_of_gt(self):
        # A sanity check — ``<`` is the "can I widen to this dep?"
        # predicate and should always be the strict inverse of ``>`` on
        # distinct inputs.
        for a in Scope:
            for b in Scope:
                if a is b:
                    continue
                assert (a > b) != (a < b)

    def test_equality_and_hash_are_stable(self):
        # IntEnum hashing equates to int hashing.
        assert hash(Scope.SINGLETON) == hash(2)
        s = {Scope.SINGLETON, Scope.REQUEST, Scope.TRANSIENT}
        assert len(s) == 3


# ---------------------------------------------------------------------------
# Stable label strings used in error messages and structured logs.
# ---------------------------------------------------------------------------


class TestScopeLabel:
    """``Scope.label`` is the stable, lowercase public string form."""

    @pytest.mark.parametrize(
        "scope,label",
        [
            (Scope.SINGLETON, "singleton"),
            (Scope.REQUEST, "request"),
            (Scope.TRANSIENT, "transient"),
        ],
    )
    def test_label_is_lowercase_name(self, scope, label):
        assert scope.label == label

    def test_str_returns_the_label(self):
        # ``str(scope)`` yields the label so log lines and format strings
        # print ``"singleton"`` rather than ``"Scope.SINGLETON"`` or the
        # numeric value.
        assert str(Scope.SINGLETON) == "singleton"

    def test_label_is_not_the_numeric_value(self):
        # ``.value`` on an IntEnum is the int; ``.label`` is the string.
        # Historically this codebase used ``.value`` to serialize scopes
        # into error details — the IntEnum migration moved that to
        # ``.label``. Regression guard.
        assert Scope.SINGLETON.value == 2
        assert Scope.SINGLETON.label == "singleton"


# ---------------------------------------------------------------------------
# The DI compiler uses comparisons directly — no bespoke lookup table.
# ---------------------------------------------------------------------------


class TestScopeViolationUsesComparison:
    """Smoke tests that the compiler's violation detector matches the
    comparison predicate."""

    def test_singleton_over_request_is_caught(self):
        @injectable(scope=Scope.REQUEST)
        class R:
            pass

        @injectable(scope=Scope.SINGLETON)
        class S:
            def __init__(self, r: R) -> None:
                self._r = r

        c = DIContainer()
        c.register(R)
        c.register(S)
        with pytest.raises(DIScopeViolationError) as excinfo:
            c.compile()
        detail = getattr(excinfo.value, "detail", {}) or {}
        # Detail strings preserved the old ``"singleton"``/``"request"``
        # API even after migrating to IntEnum.
        assert detail.get("dependent_scope") == "singleton"
        assert detail.get("dependency_scope") == "request"

    def test_error_message_names_both_scopes(self):
        @injectable(scope=Scope.TRANSIENT)
        class T:
            pass

        @injectable(scope=Scope.REQUEST)
        class R:
            def __init__(self, t: T) -> None:
                self._t = t

        c = DIContainer()
        c.register(T)
        c.register(R)
        with pytest.raises(DIScopeViolationError) as excinfo:
            c.compile()
        msg = str(excinfo.value)
        # Message conveys direction clearly.
        assert "Request" in msg
        assert "transient" in msg


# ---------------------------------------------------------------------------
# Interop: old code paths that used ``.value`` now use ``.label``, but
# the enum still compares cleanly against names and numeric fallbacks
# used in serialization layers.
# ---------------------------------------------------------------------------


class TestScopeInterop:
    def test_scope_can_be_compared_to_plain_int(self):
        # IntEnum inherits from int, so numeric comparison with bare
        # ints works — useful when deserializing scopes from JSON.
        assert Scope.SINGLETON == 2
        assert Scope.REQUEST == 1
        assert Scope.TRANSIENT == 0

    def test_from_name(self):
        # ``Scope["SINGLETON"]`` is the canonical lookup-by-name.
        assert Scope["SINGLETON"] is Scope.SINGLETON
        with pytest.raises(KeyError):
            _ = Scope["NONEXISTENT"]

    def test_from_int_value(self):
        assert Scope(0) is Scope.TRANSIENT
        assert Scope(1) is Scope.REQUEST
        assert Scope(2) is Scope.SINGLETON
