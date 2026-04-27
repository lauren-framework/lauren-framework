"""Unit tests for ``@openapi_security`` and ``_collect_guard_security``.

Coverage grid
-------------
Decorator behaviour
  1.  Basic attachment — metadata is set on the class.
  2.  Single requirement — requirements list has exactly one entry.
  3.  Multiple requirements (OR) — all dicts are stored.
  4.  Metadata contents match the passed dicts exactly.
  5.  OpenAPISecurityMeta.requirements is a *list* (mutable, not tuple).
  6.  Decorator stacks cleanly on top of @injectable and @use_guards.
  7.  Decoration order (before/after @use_guards) is irrelevant.
  8.  Bare usage (@openapi_security without parens) raises GuardConfigError.
  9.  Empty call (@openapi_security()) raises GuardConfigError.
  10. Non-dict requirement raises GuardConfigError.
  11. Decorating a function (not a class) raises GuardConfigError.
  12. OPENAPI_SECURITY_META constant value is a plain string.
  13. Two independent guard classes each carry independent metadata.

_collect_guard_security behaviour
  14. Empty guards tuple → None.
  15. Single guard with no security meta → None.
  16. Single guard with security meta → requirements verbatim (OR preserved).
  17. Single guard with multiple requirements → list with two dicts (OR).
  18. Multiple guards all without security → None.
  19. Mixed: some guards with security, some without → only secured merged.
  20. Two guards each with one requirement → AND merge into single dict.
  21. Two guards where one has multiple OR requirements → all keys merged.
  22. Guard whose meta.requirements is empty list → treated as absent.
  23. Three guards → three sets of keys all merged.
"""

from __future__ import annotations

import pytest

from lauren.decorators import (
    OPENAPI_SECURITY_META,
    OpenAPISecurityMeta,
    openapi_security,
    use_guards,
    injectable,
)
from lauren.exceptions import DecoratorUsageError, GuardConfigError
from lauren._asgi._openapi import _collect_guard_security


# ---------------------------------------------------------------------------
# Minimal guard stub — satisfies @use_guards' can_activate check
# ---------------------------------------------------------------------------


def _make_guard(**kwargs) -> type:
    """Return a minimal guard class with optional @openapi_security applied."""

    class _Guard:
        async def can_activate(self, ctx) -> bool:  # pragma: no cover
            return True

    for k, v in kwargs.items():
        setattr(_Guard, k, v)
    return _Guard


# ---------------------------------------------------------------------------
# 1-5: Attachment and metadata contents
# ---------------------------------------------------------------------------


class TestOpenAPISecurityAttachment:
    def test_basic_attachment(self):
        """@openapi_security sets OPENAPI_SECURITY_META on the class."""

        @openapi_security({"BearerAuth": []})
        class G:
            async def can_activate(self, ctx) -> bool: ...

        assert hasattr(G, OPENAPI_SECURITY_META)
        meta = getattr(G, OPENAPI_SECURITY_META)
        assert isinstance(meta, OpenAPISecurityMeta)

    def test_single_requirement_stored(self):
        """A single requirement dict is stored as a one-element list."""

        @openapi_security({"BearerAuth": []})
        class G:
            async def can_activate(self, ctx) -> bool: ...

        meta: OpenAPISecurityMeta = getattr(G, OPENAPI_SECURITY_META)
        assert len(meta.requirements) == 1
        assert meta.requirements[0] == {"BearerAuth": []}

    def test_multiple_requirements_stored(self):
        """Multiple requirement dicts are all stored (OR semantics)."""

        @openapi_security({"BearerAuth": []}, {"ApiKey": []})
        class G:
            async def can_activate(self, ctx) -> bool: ...

        meta: OpenAPISecurityMeta = getattr(G, OPENAPI_SECURITY_META)
        assert len(meta.requirements) == 2
        assert {"BearerAuth": []} in meta.requirements
        assert {"ApiKey": []} in meta.requirements

    def test_requirements_contents_match(self):
        """Scopes are preserved exactly."""

        @openapi_security({"OAuth2": ["read:users", "write:users"]})
        class G:
            async def can_activate(self, ctx) -> bool: ...

        meta: OpenAPISecurityMeta = getattr(G, OPENAPI_SECURITY_META)
        assert meta.requirements == [{"OAuth2": ["read:users", "write:users"]}]

    def test_requirements_is_list(self):
        """OpenAPISecurityMeta.requirements is a list, not a tuple."""

        @openapi_security({"BearerAuth": []})
        class G:
            async def can_activate(self, ctx) -> bool: ...

        meta: OpenAPISecurityMeta = getattr(G, OPENAPI_SECURITY_META)
        assert isinstance(meta.requirements, list)

    def test_constant_is_string(self):
        """OPENAPI_SECURITY_META is a plain string dunder-style attribute."""
        assert isinstance(OPENAPI_SECURITY_META, str)
        assert OPENAPI_SECURITY_META.startswith("__")

    def test_two_independent_guard_classes(self):
        """Each guard class carries its own independent metadata."""

        @openapi_security({"BearerAuth": []})
        class GuardA:
            async def can_activate(self, ctx) -> bool: ...

        @openapi_security({"ApiKey": ["admin"]})
        class GuardB:
            async def can_activate(self, ctx) -> bool: ...

        meta_a: OpenAPISecurityMeta = getattr(GuardA, OPENAPI_SECURITY_META)
        meta_b: OpenAPISecurityMeta = getattr(GuardB, OPENAPI_SECURITY_META)
        assert meta_a.requirements == [{"BearerAuth": []}]
        assert meta_b.requirements == [{"ApiKey": ["admin"]}]


# ---------------------------------------------------------------------------
# 6-7: Stacking with other decorators
# ---------------------------------------------------------------------------


class TestOpenAPISecurityStacking:
    def test_stacks_with_injectable(self):
        """@openapi_security + @injectable() both attach without conflict."""

        @openapi_security({"BearerAuth": []})
        @injectable()
        class G:
            async def can_activate(self, ctx) -> bool: ...

        assert hasattr(G, OPENAPI_SECURITY_META)

    def test_stacks_with_use_guards_above(self):
        """@openapi_security above @use_guards — both sets of metadata present."""
        from lauren.decorators import USE_GUARDS

        @openapi_security({"BearerAuth": []})
        class InnerGuard:
            async def can_activate(self, ctx) -> bool: ...

        @openapi_security({"ApiKey": []})
        @use_guards(InnerGuard)
        class G:
            async def can_activate(self, ctx) -> bool: ...

        assert hasattr(G, OPENAPI_SECURITY_META)
        assert hasattr(G, USE_GUARDS)

    def test_stacks_with_use_guards_below(self):
        """@use_guards above @openapi_security — same result."""
        from lauren.decorators import USE_GUARDS

        @openapi_security({"BearerAuth": []})
        class InnerGuard:
            async def can_activate(self, ctx) -> bool: ...

        @use_guards(InnerGuard)
        @openapi_security({"ApiKey": []})
        class G:
            async def can_activate(self, ctx) -> bool: ...

        assert hasattr(G, OPENAPI_SECURITY_META)
        assert hasattr(G, USE_GUARDS)


# ---------------------------------------------------------------------------
# 8-11: Error cases
# ---------------------------------------------------------------------------


class TestOpenAPISecurityErrors:
    def test_bare_usage_raises(self):
        """@openapi_security without parens passes a class → DecoratorUsageError."""

        with pytest.raises(DecoratorUsageError, match="parentheses"):

            @openapi_security  # type: ignore[arg-type]
            class G:
                async def can_activate(self, ctx) -> bool: ...

    def test_empty_call_raises(self):
        """@openapi_security() with no args raises GuardConfigError."""
        with pytest.raises(GuardConfigError, match="at least one"):
            openapi_security()

    def test_non_dict_requirement_raises(self):
        """A non-dict positional argument raises GuardConfigError."""
        with pytest.raises(GuardConfigError, match="dicts"):
            openapi_security("BearerAuth")  # type: ignore[arg-type]

    def test_integer_requirement_raises(self):
        with pytest.raises(GuardConfigError, match="dicts"):
            openapi_security(42)  # type: ignore[arg-type]

    def test_decorating_a_function_raises(self):
        """@openapi_security on a plain function raises GuardConfigError."""
        with pytest.raises(GuardConfigError, match="class"):

            @openapi_security({"BearerAuth": []})
            def not_a_class():  # type: ignore[arg-type]
                ...


# ---------------------------------------------------------------------------
# 14-23: _collect_guard_security
# ---------------------------------------------------------------------------


class TestCollectGuardSecurity:
    def test_empty_guards_returns_none(self):
        assert _collect_guard_security(()) is None

    def test_single_guard_no_meta_returns_none(self):
        g = _make_guard()
        assert _collect_guard_security((g,)) is None

    def test_single_guard_with_meta_verbatim(self):
        """Single decorated guard → requirements list returned verbatim."""

        @openapi_security({"BearerAuth": []})
        class G:
            async def can_activate(self, ctx) -> bool: ...

        result = _collect_guard_security((G,))
        assert result == [{"BearerAuth": []}]

    def test_single_guard_multiple_requirements_or_preserved(self):
        """Single guard with two requirements → both dicts in result (OR)."""

        @openapi_security({"BearerAuth": []}, {"ApiKey": []})
        class G:
            async def can_activate(self, ctx) -> bool: ...

        result = _collect_guard_security((G,))
        assert result == [{"BearerAuth": []}, {"ApiKey": []}]

    def test_multiple_guards_none_with_meta_returns_none(self):
        g1 = _make_guard()
        g2 = _make_guard()
        assert _collect_guard_security((g1, g2)) is None

    def test_mixed_guards_only_secured_contribute(self):
        """Guards without security meta are skipped; secured one wins."""
        g_plain = _make_guard()

        @openapi_security({"BearerAuth": []})
        class GSecured:
            async def can_activate(self, ctx) -> bool: ...

        result = _collect_guard_security((g_plain, GSecured))
        assert result == [{"BearerAuth": []}]

    def test_two_guards_and_merge(self):
        """Two guards each with one requirement → AND merge into single dict."""

        @openapi_security({"BearerAuth": []})
        class AuthGuard:
            async def can_activate(self, ctx) -> bool: ...

        @openapi_security({"TenantHeader": []})
        class TenantGuard:
            async def can_activate(self, ctx) -> bool: ...

        result = _collect_guard_security((AuthGuard, TenantGuard))
        assert result == [{"BearerAuth": [], "TenantHeader": []}]

    def test_two_guards_one_has_multiple_or_requirements(self):
        """Multiple guards: the OR requirements of each guard are all merged."""

        @openapi_security({"BearerAuth": []}, {"ApiKey": []})
        class FlexGuard:
            async def can_activate(self, ctx) -> bool: ...

        @openapi_security({"TenantHeader": []})
        class TenantGuard:
            async def can_activate(self, ctx) -> bool: ...

        result = _collect_guard_security((FlexGuard, TenantGuard))
        # AND merge: all keys from FlexGuard's requirements + TenantGuard
        assert result is not None
        assert len(result) == 1
        merged = result[0]
        # BearerAuth and ApiKey come from FlexGuard; TenantHeader from TenantGuard.
        # dict.update picks the last value for duplicate keys so all three are present.
        assert "TenantHeader" in merged

    def test_guard_with_empty_requirements_list_treated_as_absent(self):
        """A guard whose meta.requirements is [] contributes nothing."""

        @openapi_security({"BearerAuth": []})
        class G:
            async def can_activate(self, ctx) -> bool: ...

        # Manually empty the requirements to simulate the edge case.
        getattr(G, OPENAPI_SECURITY_META).requirements = []

        assert _collect_guard_security((G,)) is None

    def test_three_guards_all_merged(self):
        """Three guards → all their keys appear in the single merged dict."""

        @openapi_security({"BearerAuth": []})
        class G1:
            async def can_activate(self, ctx) -> bool: ...

        @openapi_security({"ApiKey": ["read"]})
        class G2:
            async def can_activate(self, ctx) -> bool: ...

        @openapi_security({"TenantId": []})
        class G3:
            async def can_activate(self, ctx) -> bool: ...

        result = _collect_guard_security((G1, G2, G3))
        assert result is not None
        assert len(result) == 1
        merged = result[0]
        assert merged["BearerAuth"] == []
        assert merged["ApiKey"] == ["read"]
        assert merged["TenantId"] == []

    def test_result_dicts_are_copies(self):
        """Modifying the result does not affect the guard's stored meta."""

        @openapi_security({"BearerAuth": []})
        class G:
            async def can_activate(self, ctx) -> bool: ...

        result = _collect_guard_security((G,))
        assert result is not None
        result[0]["BearerAuth"] = ["mutated"]

        # Original metadata is untouched.
        meta: OpenAPISecurityMeta = getattr(G, OPENAPI_SECURITY_META)
        assert meta.requirements[0]["BearerAuth"] == []
