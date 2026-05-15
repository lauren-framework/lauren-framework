"""Tests for implicit path + query parameter extraction.

Investigates the design question:

    @get("/{names}")
    async def get_names(name, q, limit): ...

and documents exactly what works, what fails at startup, and why.

Key findings
------------

1. **Path parameter auto-promotion is name-exact.**
   The route template ``/{names}`` creates a path variable called ``names``.
   A handler parameter called ``name`` (different string) is NOT matched to
   it — startup raises ``UnresolvableParameterError`` immediately.

2. **Unannotated, default-less scalar parameters cannot be resolved.**
   ``q`` and ``limit`` without type annotations and without default values
   also raise ``UnresolvableParameterError``.  The framework cannot tell
   whether they should come from the path, query string, body, or DI.

3. **The sensible design is:**
   - Parameter name must EXACTLY match the path variable name.
   - Use type annotations (``str``, ``int``, …) so the framework can
     auto-promote unmatched scalars to query parameters.
   - Use default values for optional query params.

   Correct form::

       @get("/{name}")
       async def get_user(self, name: str, q: str = "", limit: int = 10): ...

   This gives:
   - ``name`` → path (matched by name to ``{name}`` in the template)
   - ``q``    → query (typed scalar with default)
   - ``limit`` → query (typed scalar with default)
"""

from __future__ import annotations

import pytest

from lauren import (
    LaurenFactory,
    Path,
    Query,
    controller,
    get,
    module,
)
from lauren.exceptions import UnresolvableParameterError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(ctrl: type) -> TestClient:
    @module(controllers=[ctrl])
    class M:
        pass

    return TestClient(LaurenFactory.create(M))


# ---------------------------------------------------------------------------
# Failure modes — detected at startup, not at request time
# ---------------------------------------------------------------------------


class TestStartupRejection:
    def test_name_mismatch_raises_at_startup(self):
        """Path template /{names} but parameter called 'name' → startup error.

        'name' is not in path_param_names (which contains 'names'), so the
        framework cannot resolve it and raises UnresolvableParameterError.
        """
        with pytest.raises(UnresolvableParameterError, match="name"):

            @controller("/mismatch")
            class C:
                @get("/{names}")
                async def h(self, name, q, limit):
                    return {}

            @module(controllers=[C])
            class M:
                pass

            LaurenFactory.create(M)

    def test_unannotated_no_default_raises_at_startup(self):
        """Unannotated parameter without a default cannot be resolved."""
        with pytest.raises(UnresolvableParameterError):

            @controller("/bare")
            class C:
                @get("/")
                async def h(self, q):  # no annotation, no default → unresolvable
                    return {}

            @module(controllers=[C])
            class M:
                pass

            LaurenFactory.create(M)

    def test_unannotated_with_default_also_raises(self):
        """Even with a default value, an unannotated parameter fails at startup.

        ``_is_implicit_query_type`` explicitly returns False for
        ``inspect.Parameter.empty`` so that unregistered DI tokens don't
        silently become empty query params.  The annotation must be explicit.
        """
        with pytest.raises(UnresolvableParameterError):

            @controller("/defaulted")
            class C:
                @get("/")
                async def h(self, q="") -> dict:  # default but NO annotation → error
                    return {"q": q}

            @module(controllers=[C])
            class M:
                pass

            LaurenFactory.create(M)


# ---------------------------------------------------------------------------
# Correct design: name match + typed annotations
# ---------------------------------------------------------------------------


class TestCorrectDesign:
    def test_typed_path_and_query_params(self):
        """The correct form: parameter name matches path variable exactly."""

        @controller("/users")
        class C:
            @get("/{name}")
            async def get_user(self, name: str, q: str, limit: int) -> dict:
                return {"name": name, "q": q, "limit": limit}

        client = _build(C)

        r = client.get("/users/alice?q=hello&limit=5")
        assert r.status_code == 200
        assert r.json() == {"name": "alice", "q": "hello", "limit": 5}

    def test_optional_query_params_use_defaults(self):
        """Query params with defaults are optional on the wire."""

        @controller("/items")
        class C:
            @get("/{item_id}")
            async def get_item(self, item_id: str, q: str = "", limit: int = 10) -> dict:
                return {"item_id": item_id, "q": q, "limit": limit}

        client = _build(C)
        r = client.get("/items/widget")  # no query string
        assert r.status_code == 200
        assert r.json() == {"item_id": "widget", "q": "", "limit": 10}

    def test_path_int_coercion(self):
        """Path variables can be typed as int — the framework coerces."""

        @controller("/records")
        class C:
            @get("/{record_id}")
            async def get_record(self, record_id: int, verbose: bool = False) -> dict:
                return {"record_id": record_id, "verbose": verbose}

        client = _build(C)
        r = client.get("/records/42?verbose=true")
        assert r.status_code == 200
        assert r.json() == {"record_id": 42, "verbose": True}

    def test_explicit_markers_override_auto_promotion(self):
        """Explicit Path[T] and Query[T] markers work regardless of name."""

        @controller("/explicit")
        class C:
            @get("/{names}")
            async def h(
                self,
                names: Path[str],  # explicit — 'names' matches path var
                q: Query[str] = "",  # explicit — always query
                limit: Query[int] = 10,
            ) -> dict:
                return {"names": names, "q": q, "limit": limit}

        client = _build(C)
        r = client.get("/explicit/alice?q=test&limit=3")
        assert r.status_code == 200
        assert r.json() == {"names": "alice", "q": "test", "limit": 3}

    def test_multiple_path_params(self):
        """Multiple path variables, all matched by name."""

        @controller("/orgs")
        class C:
            @get("/{org}/{repo}")
            async def h(self, org: str, repo: str, branch: str = "main") -> dict:
                return {"org": org, "repo": repo, "branch": branch}

        client = _build(C)
        r = client.get("/orgs/acme/myrepo?branch=dev")
        assert r.status_code == 200
        assert r.json() == {"org": "acme", "repo": "myrepo", "branch": "dev"}

    def test_required_query_param_without_default(self):
        """Typed scalar without a default is a required query param."""

        @controller("/search")
        class C:
            @get("/")
            async def h(self, q: str) -> dict:  # no default → required
                return {"q": q}

        client = _build(C)
        r = client.get("/search/?q=python")
        assert r.status_code == 200
        assert r.json() == {"q": "python"}

        r_missing = client.get("/search/")  # missing required param
        assert r_missing.status_code == 422  # ExtractorFieldError


# ---------------------------------------------------------------------------
# Design summary as tests
# ---------------------------------------------------------------------------


class TestDesignSummary:
    def test_original_design_question_corrected(self):
        """The original question corrected to a working form.

        Original (broken):
            @get("/{names}") async def get_names(name, q, limit): ...
            # name ≠ names → UnresolvableParameterError
            # q, limit unannotated without defaults → UnresolvableParameterError

        Corrected form 1 — match the path variable name:
            @get("/{name}") async def get_names(name: str, q: str = "", limit: int = 10)

        Corrected form 2 — use explicit markers to decouple name from var:
            @get("/{names}") async def get_names(names: str, q: str = "", limit: int = 10)
        """

        # Form 1: name matches path variable
        @controller("/form1")
        class Form1:
            @get("/{name}")
            async def get_names(self, name: str, q: str = "", limit: int = 10) -> dict:
                return {"name": name, "q": q, "limit": limit}

        # Form 2: names matches path variable
        @controller("/form2")
        class Form2:
            @get("/{names}")
            async def get_names(self, names: str, q: str = "", limit: int = 10) -> dict:
                return {"names": names, "q": q, "limit": limit}

        @module(controllers=[Form1, Form2])
        class M:
            pass

        client = TestClient(LaurenFactory.create(M))

        r1 = client.get("/form1/alice?q=hi&limit=5")
        assert r1.json() == {"name": "alice", "q": "hi", "limit": 5}

        r2 = client.get("/form2/alice?q=hi&limit=5")
        assert r2.json() == {"names": "alice", "q": "hi", "limit": 5}
