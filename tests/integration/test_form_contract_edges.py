"""Form transport edge cases retain native types and startup diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, NamedTuple

import pytest

from lauren import Form, LaurenFactory, UnresolvableParameterError, UploadField, controller, module, post
from lauren.testing import TestClient


def _app(model):
    @controller("/edges")
    class Edges:
        @post("/")
        async def create(self, form: Form[model]) -> dict:
            return {"type": type(form).__name__}

    @module(controllers=[Edges])
    class Root:
        pass

    return LaurenFactory.create(Root)


class Search(NamedTuple):
    query: str
    limit: int = 10


def test_namedtuple_forms_use_field_defaults_and_typed_values() -> None:
    with TestClient(_app(Search)) as client:
        response = client.post("/edges", data={"query": "example"})
    assert response.json() == {"type": "Search"}


@dataclass
class BlankFields:
    choice: Literal["", "a"]
    number_or_text: int | str


def test_blank_literal_and_string_union_members_are_not_omitted() -> None:
    with TestClient(_app(BlankFields)) as client:
        response = client.post("/edges", data={"choice": "", "number_or_text": ""})
    assert response.status_code == 200


def test_upload_descriptor_on_a_text_model_field_fails_startup() -> None:
    @dataclass
    class Invalid:
        name: Annotated[str, UploadField(max_size=4)]

    with pytest.raises(UnresolvableParameterError) as caught:
        _app(Invalid)
    assert caught.value.detail["field"] == "name"


@pytest.mark.parametrize("path", [False, True])
def test_non_flat_pydantic_form_alias_is_rejected_at_startup(path) -> None:
    pydantic = pytest.importorskip("pydantic")
    alias = pydantic.AliasPath("nested", "name") if path else pydantic.AliasChoices("name", "other")

    class Aliased(pydantic.BaseModel):
        name: str = pydantic.Field(validation_alias=alias)

    with pytest.raises(UnresolvableParameterError) as caught:
        _app(Aliased)
    assert caught.value.detail["reason"] == "unsupported_form_alias"


def test_pydantic_field_metadata_stays_part_of_form_validation() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Bounded(pydantic.BaseModel):
        count: int = pydantic.Field(ge=2)

    with TestClient(_app(Bounded)) as client:
        rejected = client.post("/edges", data={"count": "1"})
        accepted = client.post("/edges", data={"count": "2"})
    assert rejected.status_code == 422
    assert accepted.status_code == 200


def test_unsupported_form_field_type_fails_startup_with_context() -> None:
    class Unsupported:
        pass

    @dataclass
    class Invalid:
        value: Unsupported

    with pytest.raises(UnresolvableParameterError) as caught:
        _app(Invalid)
    assert caught.value.detail["param"] == "form"
    assert caught.value.detail["reason"] == "unsupported_type"


@pytest.mark.parametrize("media", ["application/json", "text/plain", "*/*"])
def test_typed_forms_reject_unparseable_consumes_at_startup(media) -> None:
    from lauren import StartupError

    @controller("/bad-form-media")
    class Invalid:
        @post("/", consumes=media)
        async def create(self, name: Form[str]) -> dict:
            return {"name": name}

    @module(controllers=[Invalid])
    class Root:
        pass

    with pytest.raises(StartupError) as caught:
        LaurenFactory.create(Root)
    assert caught.value.detail["reason"] == "invalid_form_consumes"
