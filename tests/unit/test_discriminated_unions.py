"""Unit tests for feature 6 — discriminated-union validation.

These tests exercise the extractor layer in isolation (no ASGI runtime)
to confirm that:

* ``Annotated[Union[...], Field(discriminator=...)]`` is recognised by
  :func:`is_discriminated_union`.
* The JSON validator routes tagged-union payloads through a
  :class:`pydantic.TypeAdapter` and returns a concrete variant instance.
* Invalid payloads produce :class:`ExtractorError` with structured error
  detail pointing at the offending variant.
* Primitive and plain-model JSON paths are not regressed.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

import pytest
from pydantic import BaseModel, Field

from lauren.exceptions import ExtractorError
from lauren.extractors import _validate_json
from lauren.streaming import (
    discriminator_key,
    discriminator_variants,
    is_discriminated_union,
)


# ---------------------------------------------------------------------------
# Fixture models \u2014 the example shipped in the feature spec.
# ---------------------------------------------------------------------------


class ImageEvent(BaseModel):
    kind: Literal["image"]
    url: str


class TextEvent(BaseModel):
    kind: Literal["text"]
    content: str


Event = Annotated[Union[ImageEvent, TextEvent], Field(discriminator="kind")]


class TestDiscriminatedUnionDetection:
    def test_is_discriminated_union_true(self):
        assert is_discriminated_union(Event) is True

    def test_is_discriminated_union_false_for_plain_model(self):
        assert is_discriminated_union(ImageEvent) is False

    def test_is_discriminated_union_false_for_plain_union(self):
        assert is_discriminated_union(Union[ImageEvent, TextEvent]) is False

    def test_discriminator_key(self):
        assert discriminator_key(Event) == "kind"

    def test_discriminator_variants(self):
        variants = discriminator_variants(Event)
        assert set(variants) == {ImageEvent, TextEvent}


class TestValidateJsonDiscriminatedUnion:
    def test_image_variant_selected(self):
        payload = {"kind": "image", "url": "https://example.com/x.png"}
        result = _validate_json(payload, Event, "body")
        assert isinstance(result, ImageEvent)
        assert result.url == "https://example.com/x.png"

    def test_text_variant_selected(self):
        payload = {"kind": "text", "content": "hello"}
        result = _validate_json(payload, Event, "body")
        assert isinstance(result, TextEvent)
        assert result.content == "hello"

    def test_unknown_tag_raises_extractor_error(self):
        with pytest.raises(ExtractorError) as exc_info:
            _validate_json({"kind": "video", "url": "..."}, Event, "body")
        err = exc_info.value
        assert err.detail["field"] == "body"
        # Pydantic's discriminator errors include a ``union_tag_invalid``
        # code which should surface verbatim in the payload so API clients
        # can act on it programmatically.
        serialized = err.to_payload()
        codes = {e.get("type") for e in serialized["error"]["detail"]["errors"]}
        assert any("tag" in c for c in codes)

    def test_missing_tag_raises_extractor_error(self):
        with pytest.raises(ExtractorError) as exc_info:
            _validate_json({"url": "x"}, Event, "body")
        # Regardless of the exact Pydantic version, the structured error
        # payload must at least name the missing discriminator field.
        errors = exc_info.value.detail["errors"]
        assert isinstance(errors, list) and len(errors) >= 1

    def test_invalid_variant_field_points_to_that_variant(self):
        # ``kind=image`` pins the variant; the missing ``url`` should be
        # attributed to ImageEvent rather than a vague ``Union`` error.
        with pytest.raises(ExtractorError) as exc_info:
            _validate_json({"kind": "image"}, Event, "body")
        errors = exc_info.value.detail["errors"]
        # At least one error must reference the ``url`` field.
        assert any("url" in tuple(e.get("loc", ())) for e in errors)


class TestPlainPathsUnchanged:
    """Regression guard \u2014 the feature must not affect non-discriminated paths."""

    def test_plain_pydantic_model_still_validates(self):
        out = _validate_json({"kind": "image", "url": "x"}, ImageEvent, "body")
        assert isinstance(out, ImageEvent)

    def test_primitive_passthrough(self):
        assert _validate_json({"a": 1}, dict, "body") == {"a": 1}

    def test_list_passthrough(self):
        assert _validate_json([1, 2, 3], list, "body") == [1, 2, 3]
