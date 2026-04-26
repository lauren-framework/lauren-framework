"""End-to-end tests for the RFC 7807 Problem Details error envelope.

These tests build real :class:`LaurenApp` instances via
:meth:`LaurenFactory.create` and verify that:

* Without opt-in, errors still emit the classic lauren envelope.
* ``error_format='rfc7807'`` switches every error response to the
  Problem Details shape with ``application/problem+json`` content
  type.
* All four required RFC 7807 fields (``type``, ``title``, ``status``,
  ``detail``) are present on every error.
* The extension ``code`` field carries the lauren-specific machine
  identifier alongside the standard fields.
* The extension ``errors`` field carries the original structured
  ``detail`` dict so machine clients don't lose information.
* Built-in errors (``RouteNotFoundError``, ``MethodNotAllowedError``,
  ``ExtractorFieldError``) all map correctly.
* Custom subclasses can override ``problem_type`` and
  ``problem_title`` for a richer problem catalogue.
* Unknown ``error_format`` values fall back to ``'default'`` rather
  than raising at startup.
"""

from __future__ import annotations

import asyncio
import json


from lauren import (
    LaurenFactory,
    Path,
    controller,
    get,
    module,
)
from lauren.exceptions import HTTPError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Handler fixtures
# ---------------------------------------------------------------------------


class _Teapot(HTTPError):
    status_code = 418
    code = "teapot"


class _CustomProblem(HTTPError):
    status_code = 422
    code = "invalid_quantum_state"
    # Custom problem type URI \u2014 what RFC 7807 encourages: a
    # dereferenceable URL that documents the problem.
    problem_type = "https://example.com/problems/quantum-state"
    problem_title = "Quantum State Invalidated"


@controller("/errors")
class _ErrController:
    @get("/teapot")
    async def teapot(self) -> dict:
        raise _Teapot("I'm a teapot", detail={"reason": "brewing"})

    @get("/custom")
    async def custom(self) -> dict:
        raise _CustomProblem(
            "wave function collapsed",
            detail={"particles": 7, "observer": "schrodinger"},
        )

    @get("/users/{user_id}")
    async def user(self, user_id: Path[int]) -> dict:
        return {"id": user_id}


@module(controllers=[_ErrController])
class _ErrModule:
    pass


# ---------------------------------------------------------------------------
# 1. Default format unchanged (backwards compatibility)
# ---------------------------------------------------------------------------


def test_default_format_emits_classic_envelope() -> None:
    app = asyncio.run(LaurenFactory.create(_ErrModule))
    r = TestClient(app).get("/errors/teapot")
    assert r.status_code == 418
    assert r.header(
        "content-type",
    ).startswith("application/json")
    payload = r.json()
    assert payload == {
        "error": {
            "code": "teapot",
            "message": "I'm a teapot",
            "detail": {"reason": "brewing"},
        }
    }


def test_default_format_not_problem_json_content_type() -> None:
    """Backwards compat: default envelope must NOT claim to be\n    problem+json, because existing clients parsing the classic\n    shape would misinterpret the content type.\n"""
    app = asyncio.run(LaurenFactory.create(_ErrModule))
    r = TestClient(app).get("/errors/teapot")
    assert "problem" not in (r.header("content-type") or "")


# ---------------------------------------------------------------------------
# 2. RFC 7807 format \u2014 every required field present
# ---------------------------------------------------------------------------


def test_rfc7807_envelope_has_required_fields() -> None:
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    r = TestClient(app).get("/errors/teapot")
    assert r.status_code == 418
    payload = r.json()
    # RFC 7807 \u00a73.1 mandates type / title / status / detail.
    assert "type" in payload
    assert "title" in payload
    assert "status" in payload
    assert "detail" in payload
    assert payload["status"] == 418


def test_rfc7807_content_type_is_problem_json() -> None:
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    r = TestClient(app).get("/errors/teapot")
    assert (r.header("content-type") or "").startswith("application/problem+json")


def test_rfc7807_default_type_is_iana_urn() -> None:
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    r = TestClient(app).get("/errors/teapot")
    payload = r.json()
    # The framework's default ``type`` is a stable URN that does not
    # require a live HTTP URL.
    assert payload["type"] == "urn:ietf:rfc:7231:418"


def test_rfc7807_default_title_is_iana_reason_phrase() -> None:
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    r = TestClient(app).get("/errors/teapot")
    payload = r.json()
    # "I'm a Teapot" is the IANA reason phrase for 418.
    assert payload["title"] == "I'm a Teapot"


def test_rfc7807_includes_lauren_code_extension() -> None:
    """RFC 7807 \u00a73.2 allows extensions. lauren adds ``code`` so\n    clients can match on the machine-readable error identifier\n    without scraping ``type``.\n"""
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    r = TestClient(app).get("/errors/teapot")
    payload = r.json()
    assert payload["code"] == "teapot"


def test_rfc7807_includes_detail_structured_extension() -> None:
    """Original structured detail dict rides alongside as ``errors``\n    so machine clients keep their full context (field names, etc.).\n"""
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    r = TestClient(app).get("/errors/teapot")
    payload = r.json()
    assert payload["errors"] == {"reason": "brewing"}


# ---------------------------------------------------------------------------
# 3. Custom problem type / title overrides
# ---------------------------------------------------------------------------


def test_rfc7807_honours_custom_problem_type_uri() -> None:
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    r = TestClient(app).get("/errors/custom")
    payload = r.json()
    assert payload["type"] == "https://example.com/problems/quantum-state"
    assert payload["title"] == "Quantum State Invalidated"
    assert payload["status"] == 422
    assert payload["code"] == "invalid_quantum_state"


def test_rfc7807_detail_falls_back_to_title_when_message_empty() -> None:
    """When the exception was raised with an empty message, the\n    ``detail`` field defaults to the title so the response is\n    never missing the human-readable summary required by\n    RFC 7807.\n"""

    class _Silent(HTTPError):
        status_code = 500
        code = "silent"

    @controller("/silent")
    class _SilentCtrl:
        @get("/")
        async def h(self) -> dict:
            raise _Silent()  # empty message

    @module(controllers=[_SilentCtrl])
    class _SilentMod:
        pass

    app = asyncio.run(LaurenFactory.create(_SilentMod, error_format="rfc7807"))
    r = TestClient(app).get("/silent/")
    payload = r.json()
    # Detail falls back to the IANA reason phrase for 500.
    assert payload["detail"] == payload["title"]
    assert payload["title"] == "Internal Server Error"


# ---------------------------------------------------------------------------
# 4. Built-in framework errors
# ---------------------------------------------------------------------------


def test_rfc7807_route_not_found() -> None:
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    r = TestClient(app).get("/totally/unknown")
    assert r.status_code == 404
    payload = r.json()
    assert payload["status"] == 404
    assert payload["title"] == "Not Found"
    assert payload["code"] == "route_not_found"


def test_rfc7807_method_not_allowed() -> None:
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    r = TestClient(app).post("/errors/teapot")
    assert r.status_code == 405
    payload = r.json()
    assert payload["status"] == 405
    assert payload["title"] == "Method Not Allowed"
    # ``Allow`` header still set alongside the problem response.
    assert r.header("allow") == "GET"


def test_rfc7807_extractor_validation_error() -> None:
    """``/users/abc`` triggers a Path[int] coercion failure which\n    maps to an ``ExtractorFieldError`` with HTTP 422. The RFC\n    7807 shape must carry the underlying detail dict under the\n    ``errors`` extension.\n"""
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    r = TestClient(app).get("/errors/users/not-a-number")
    assert r.status_code == 422
    payload = r.json()
    assert payload["status"] == 422
    assert payload["code"].startswith("extractor_")


# ---------------------------------------------------------------------------
# 5. Unknown error_format falls back gracefully
# ---------------------------------------------------------------------------


def test_unknown_error_format_falls_back_to_default() -> None:
    """A typo in ``error_format`` must not kill startup. The\n    framework logs a warning and falls back to the classic\n    envelope so apps keep serving.\n"""
    app = asyncio.run(LaurenFactory.create(_ErrModule, error_format="problem+json"))
    # Framework silently corrected the value to "default".
    assert app.error_format == "default"
    r = TestClient(app).get("/errors/teapot")
    assert "error" in r.json()  # classic envelope
    assert "type" not in r.json()  # not RFC 7807


# ---------------------------------------------------------------------------
# 6. RFC 7807 payload is still valid JSON even with unicode details
# ---------------------------------------------------------------------------


def test_rfc7807_preserves_unicode_in_detail() -> None:
    class _Unicode(HTTPError):
        status_code = 400
        code = "unicode_err"

    @controller("/u")
    class _UCtrl:
        @get("/")
        async def h(self) -> dict:
            raise _Unicode("caf\u00e9 not found", detail={"q": "\u65e5\u672c"})

    @module(controllers=[_UCtrl])
    class _UMod:
        pass

    app = asyncio.run(LaurenFactory.create(_UMod, error_format="rfc7807"))
    r = TestClient(app).get("/u/")
    # Body must decode cleanly and preserve every character.
    payload = json.loads(r.body)
    assert payload["detail"] == "caf\u00e9 not found"
    assert payload["errors"] == {"q": "\u65e5\u672c"}


# ---------------------------------------------------------------------------
# 7. error_format property exposes the configured value
# ---------------------------------------------------------------------------


def test_error_format_property_exposes_configuration() -> None:
    app_default = asyncio.run(LaurenFactory.create(_ErrModule))
    assert app_default.error_format == "default"
    app_rfc = asyncio.run(LaurenFactory.create(_ErrModule, error_format="rfc7807"))
    assert app_rfc.error_format == "rfc7807"
