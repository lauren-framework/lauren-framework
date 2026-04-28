"""Integration tests for feature 6 \u2014 discriminated unions end-to-end.

A real :class:`LaurenApp` is assembled with a handler that accepts
``Json[Event]`` and returns ``Event``. The tests drive the app through
:class:`~lauren.testing.TestClient` to verify:

* Happy-path validation + variant-aware dispatch via ``match``.
* Wrong-tag / missing-tag / wrong-shape payloads produce 422s with
  structured error detail.
* The generated OpenAPI document advertises the request body and
  response schemas as ``oneOf`` + ``discriminator.mapping``.
* Returning a variant model serializes through the same path as plain
  Pydantic models (no regression of the auto-serializer).
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from lauren import (
    Json,
    LaurenFactory,
    controller,
    get,
    module,
    post,
)
from lauren.testing import TestClient


class ImageEvent(BaseModel):
    kind: Literal["image"]
    url: str


class TextEvent(BaseModel):
    kind: Literal["text"]
    content: str


Event = Annotated[Union[ImageEvent, TextEvent], Field(discriminator="kind")]


@controller("/events", tags=["events"])
class EventsController:
    @post("/", response_model=Event)
    async def create(self, body: Json[Event]) -> Event:
        # The spec's canonical consumer pattern: structural pattern match
        # on the validated variant. The framework guarantees ``body`` is
        # already a concrete ``ImageEvent`` or ``TextEvent``.
        match body:
            case ImageEvent(url=u):
                return ImageEvent(kind="image", url=u.upper())
            case TextEvent(content=c):
                return TextEvent(kind="text", content=c.upper())

    @get("/echo-last", response_model=Event)
    async def echo_last(self) -> Event:
        # Purely for OpenAPI surface coverage \u2014 response-only discriminated
        # union (no request body). This proves the generator handles both
        # directions symmetrically.
        return TextEvent(kind="text", content="fixed")


@module(controllers=[EventsController])
class EventsModule:
    pass


def _app():
    return LaurenFactory.create(EventsModule, openapi_url="/openapi.json")


class TestDiscriminatedUnionDispatch:
    def test_image_variant_dispatch(self):
        client = TestClient(_app())
        r = client.post(
            "/events/",
            json={"kind": "image", "url": "https://example.com/x.png"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {
            "kind": "image",
            "url": "HTTPS://EXAMPLE.COM/X.PNG",
        }

    def test_text_variant_dispatch(self):
        client = TestClient(_app())
        r = client.post("/events/", json={"kind": "text", "content": "hi"})
        assert r.status_code == 200, r.text
        assert r.json() == {"kind": "text", "content": "HI"}


class TestDiscriminatedUnionErrors:
    def test_unknown_tag_yields_422(self):
        client = TestClient(_app())
        r = client.post("/events/", json={"kind": "video", "url": "..."})
        assert r.status_code == 422
        payload = r.json()
        assert payload["error"]["code"] == "extractor_error"
        assert payload["error"]["detail"]["field"] == "body"
        # Pydantic attaches a ``union_tag_invalid`` (or similar) error code
        # plus a list of valid tags so clients can self-correct.
        errs = payload["error"]["detail"]["errors"]
        assert any("tag" in str(e.get("type", "")).lower() for e in errs)

    def test_missing_tag_yields_422(self):
        client = TestClient(_app())
        r = client.post("/events/", json={"url": "x"})
        assert r.status_code == 422
        errs = r.json()["error"]["detail"]["errors"]
        assert errs  # at least one error

    def test_wrong_variant_field_attributed_correctly(self):
        # ``kind=image`` pins the variant; the missing ``url`` must surface
        # as an ImageEvent-local error, not a vague Union-level one.
        client = TestClient(_app())
        r = client.post("/events/", json={"kind": "image"})
        assert r.status_code == 422
        errs = r.json()["error"]["detail"]["errors"]
        locs = [tuple(e.get("loc", ())) for e in errs]
        assert any("url" in loc for loc in locs)


class TestDiscriminatedUnionOpenAPI:
    def test_request_body_uses_oneof_with_discriminator(self):
        app = _app()
        client = TestClient(app)
        doc = client.get("/openapi.json").json()
        op = doc["paths"]["/events"]["post"]
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        assert "oneOf" in schema
        # Both variants must be referenced via $ref.
        refs = {entry["$ref"] for entry in schema["oneOf"]}
        assert "#/components/schemas/ImageEvent" in refs
        assert "#/components/schemas/TextEvent" in refs
        # discriminator + mapping must be present.
        disc = schema["discriminator"]
        assert disc["propertyName"] == "kind"
        mapping = disc["mapping"]
        assert mapping["image"] == "#/components/schemas/ImageEvent"
        assert mapping["text"] == "#/components/schemas/TextEvent"

    def test_response_schema_uses_oneof_with_discriminator(self):
        app = _app()
        client = TestClient(app)
        doc = client.get("/openapi.json").json()
        op = doc["paths"]["/events"]["post"]
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        assert "oneOf" in schema
        assert schema["discriminator"]["propertyName"] == "kind"

    def test_response_only_route_still_emits_oneof(self):
        app = _app()
        client = TestClient(app)
        doc = client.get("/openapi.json").json()
        op = doc["paths"]["/events/echo-last"]["get"]
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        assert "oneOf" in schema

    def test_both_variant_schemas_registered_in_components(self):
        app = _app()
        client = TestClient(app)
        doc = client.get("/openapi.json").json()
        schemas = doc["components"]["schemas"]
        assert "ImageEvent" in schemas
        assert "TextEvent" in schemas
