"""Hypothesis-based property tests for the validation dispatch layer."""

import dataclasses
import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, assume  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


@dataclasses.dataclass
class SimpleModel:
    name: str
    count: int


class TestValidateAsProperties:
    @given(
        name=st.text(min_size=1, max_size=100),
        count=st.integers(min_value=0, max_value=10_000),
    )
    def test_valid_data_always_succeeds(self, name: str, count: int):
        from lauren._validation import validate_as

        result = validate_as(SimpleModel, {"name": name, "count": count}, field="body")
        assert isinstance(result, SimpleModel)
        assert result.name == name
        assert result.count == count

    @given(data=st.fixed_dictionaries({"name": st.text(min_size=1)}))
    def test_missing_required_field_raises(self, data: dict):
        from lauren._validation import validate_as
        from lauren.exceptions import ExtractorError

        # count is required and missing, so this should raise
        with pytest.raises(ExtractorError):
            validate_as(SimpleModel, data, field="body")

    @given(
        payload=st.one_of(
            st.integers(),
            st.floats(allow_nan=False),
            st.text(),
            st.none(),
            st.lists(st.integers()),
        )
    )
    def test_non_dict_always_raises(self, payload):
        from lauren._validation import validate_as

        # Non-dict payloads must never produce a successfully validated SimpleModel
        with pytest.raises(Exception):
            validate_as(SimpleModel, payload, field="body")


class TestDiscriminatedProperties:
    @given(
        name=st.text(min_size=1, max_size=50),
        kind=st.sampled_from(["cat", "dog"]),
    )
    def test_valid_discriminated_always_succeeds(self, name: str, kind: str):
        from typing import Literal
        from lauren import Discriminated
        from lauren._discriminated import validate_native_discriminated

        @dataclasses.dataclass
        class Cat:
            kind: Literal["cat"] = "cat"
            name: str = ""

        @dataclasses.dataclass
        class Dog:
            kind: Literal["dog"] = "dog"
            name: str = ""

        DType = Discriminated[Cat | Dog, "kind"]
        result = validate_native_discriminated({"kind": kind, "name": name}, DType, "body")
        assert type(result).__name__.lower() == kind

    @given(kind=st.text().filter(lambda s: s not in ("cat", "dog")))
    def test_unknown_tag_always_raises(self, kind: str):
        from typing import Literal
        from lauren import Discriminated
        from lauren._discriminated import validate_native_discriminated
        from lauren.exceptions import ExtractorError

        @dataclasses.dataclass
        class Cat:
            kind: Literal["cat"] = "cat"

        @dataclasses.dataclass
        class Dog:
            kind: Literal["dog"] = "dog"

        DType = Discriminated[Cat | Dog, "kind"]
        assume(kind != "cat" and kind != "dog")
        with pytest.raises(ExtractorError):
            validate_native_discriminated({"kind": kind}, DType, "body")
