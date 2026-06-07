"""Unit tests for Phase 4 native discriminated unions (lauren._discriminated)."""

import dataclasses
from typing import Literal, TypedDict

import pytest

from lauren import Discriminated
from lauren._discriminated import (
    get_discriminator_marker,
    is_native_discriminated_union,
    openapi_schema_for_discriminated,
    validate_native_discriminated,
)


@dataclasses.dataclass
class Cat:
    kind: Literal["cat"] = "cat"
    name: str = ""


@dataclasses.dataclass
class Dog:
    kind: Literal["dog"] = "dog"
    name: str = ""


AnimalType = Discriminated[Cat | Dog, "kind"]


class TestDiscriminatedType:
    def test_builds_annotated_type(self):
        assert is_native_discriminated_union(AnimalType)

    def test_marker_has_correct_key(self):
        marker = get_discriminator_marker(AnimalType)
        assert marker is not None
        assert marker.key == "kind"

    def test_marker_mapping_has_both_variants(self):
        marker = get_discriminator_marker(AnimalType)
        assert marker is not None
        assert set(marker.mapping.keys()) == {"cat", "dog"}
        assert marker.mapping["cat"] is Cat
        assert marker.mapping["dog"] is Dog

    def test_plain_union_is_not_discriminated(self):
        from typing import Union

        assert not is_native_discriminated_union(Union[Cat, Dog])

    def test_plain_type_is_not_discriminated(self):
        assert not is_native_discriminated_union(Cat)

    def test_wrong_argument_count_raises(self):
        with pytest.raises(TypeError):
            _ = Discriminated[Cat]  # type: ignore[index]

    def test_non_string_key_raises(self):
        with pytest.raises(TypeError):
            _ = Discriminated[Cat | Dog, 42]  # type: ignore[index]

    def test_typing_union_syntax(self):
        from typing import Union

        t = Discriminated[Union[Cat, Dog], "kind"]
        assert is_native_discriminated_union(t)

    def test_missing_discriminator_field_raises_type_error(self):
        @dataclasses.dataclass
        class NoTag:
            name: str = ""

        with pytest.raises(TypeError, match="no Literal annotation"):
            _ = Discriminated[NoTag | Cat, "kind"]


class TestValidateNativeDiscriminated:
    def test_dispatches_to_cat(self):
        result = validate_native_discriminated({"kind": "cat", "name": "Mittens"}, AnimalType, "body")
        assert isinstance(result, Cat)
        assert result.name == "Mittens"

    def test_dispatches_to_dog(self):
        result = validate_native_discriminated({"kind": "dog", "name": "Rex"}, AnimalType, "body")
        assert isinstance(result, Dog)
        assert result.name == "Rex"

    def test_missing_discriminator_field_raises(self):
        from lauren.exceptions import ExtractorError

        with pytest.raises(ExtractorError) as exc_info:
            validate_native_discriminated({"name": "Rex"}, AnimalType, "body")
        assert "kind" in str(exc_info.value.detail["errors"])

    def test_unknown_tag_raises(self):
        from lauren.exceptions import ExtractorError

        with pytest.raises(ExtractorError) as exc_info:
            validate_native_discriminated({"kind": "fish", "name": "Nemo"}, AnimalType, "body")
        assert "fish" in str(exc_info.value.detail["errors"])
        assert "cat" in str(exc_info.value.detail["errors"])

    def test_non_dict_raises(self):
        from lauren.exceptions import ExtractorError

        with pytest.raises(ExtractorError):
            validate_native_discriminated("not-a-dict", AnimalType, "body")

    def test_non_dict_error_mentions_type(self):
        from lauren.exceptions import ExtractorError

        with pytest.raises(ExtractorError) as exc_info:
            validate_native_discriminated(42, AnimalType, "body")
        assert "int" in str(exc_info.value.detail["errors"])

    def test_typeddict_variant_dispatches(self):
        class DepositEvent(TypedDict):
            event: Literal["deposit"]
            amount: float

        class WithdrawEvent(TypedDict):
            event: Literal["withdraw"]
            amount: float

        EventType = Discriminated[DepositEvent | WithdrawEvent, "event"]
        result = validate_native_discriminated({"event": "deposit", "amount": 100.0}, EventType, "body")
        assert result == {"event": "deposit", "amount": 100.0}


class TestOpenAPIForDiscriminated:
    def test_produces_oneof(self):
        components: dict = {}
        schema = openapi_schema_for_discriminated(AnimalType, components)
        assert "oneOf" in schema
        assert len(schema["oneOf"]) == 2

    def test_produces_discriminator_block(self):
        components: dict = {}
        schema = openapi_schema_for_discriminated(AnimalType, components)
        assert "discriminator" in schema
        assert schema["discriminator"]["propertyName"] == "kind"

    def test_variant_schemas_added_to_components(self):
        components: dict = {}
        openapi_schema_for_discriminated(AnimalType, components)
        assert "Cat" in components
        assert "Dog" in components

    def test_variant_schema_is_valid_json_schema(self):
        components: dict = {}
        openapi_schema_for_discriminated(AnimalType, components)
        cat_schema = components["Cat"]
        assert cat_schema["type"] == "object"
        assert "kind" in cat_schema["properties"]
        assert "name" in cat_schema["properties"]

    def test_discriminator_mapping_refs(self):
        components: dict = {}
        schema = openapi_schema_for_discriminated(AnimalType, components)
        mapping = schema["discriminator"]["mapping"]
        assert mapping["cat"] == "#/components/schemas/Cat"
        assert mapping["dog"] == "#/components/schemas/Dog"

    def test_non_discriminated_returns_empty(self):
        schema = openapi_schema_for_discriminated(Cat, {})
        assert schema == {}
