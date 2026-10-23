"""Unit tests for the validation issue model and the backend error translation."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from lauren._schema._backends import msgspec_loc, translate_msgspec, translate_pydantic
from lauren._schema._issues import (
    MAX_MESSAGE_LENGTH,
    MISSING,
    format_expected,
    format_loc,
    jsonable,
    make_issue,
)
from lauren._schema._params import param_loc_root, synthesize_issues
from lauren.exceptions import ExtractorError, ExtractorFieldError
from lauren.extractors import Extraction, FieldDescriptor
from lauren.validation import ValidationIssue
from lauren.websockets import WebSocketValidationError


def test_issue_dict_omits_input_and_empty_ctx_by_default() -> None:
    issue = ValidationIssue(("query", "page"), "int_parsing", "bad", input="abc")

    assert issue.to_dict() == {"loc": ["query", "page"], "type": "int_parsing", "msg": "bad"}


def test_issue_dict_includes_input_on_request() -> None:
    issue = ValidationIssue(("body", "when"), "x", "m", input=datetime.date(2024, 1, 2), ctx={"le": 100})

    assert issue.to_dict(include_input=True) == {
        "loc": ["body", "when"],
        "type": "x",
        "msg": "m",
        "input": "2024-01-02",
        "ctx": {"le": 100},
    }


def test_issue_without_input_never_echoes_one() -> None:
    assert "input" not in ValidationIssue(("body",), "missing", "m").to_dict(include_input=True)


def test_issue_text_and_prefix() -> None:
    issue = ValidationIssue(("age",), "int_type", "Input should be a valid integer")

    assert str(issue.prefixed("body")) == "body.age: Input should be a valid integer"
    assert format_loc(()) == "<root>"


def test_messages_are_bounded() -> None:
    issue = make_issue("value_error", msg="x" * 2000)

    assert len(issue.msg) == MAX_MESSAGE_LENGTH


def test_message_templates_pluralise_lengths() -> None:
    assert (
        make_issue("string_too_short", ctx={"min_length": 1}).msg == "String should have at least 1 character"
    )
    assert (
        make_issue("string_too_short", ctx={"min_length": 2}).msg
        == "String should have at least 2 characters"
    )


def test_missing_template_fields_fall_back_to_the_template() -> None:
    assert make_issue("greater_than", ctx={}).msg == "Input should be greater than {gt}"


def test_format_expected() -> None:
    assert format_expected([]) == "nothing"
    assert format_expected(["a"]) == "'a'"
    assert format_expected(["a", "b", "c"]) == "'a', 'b' or 'c'"


def test_jsonable_converts_nested_values() -> None:
    assert jsonable({"a": (1, b"x", {2})}) == {"a": [1, "x", [2]]}
    assert repr(MISSING) == "MISSING" and not MISSING


# ---------------------------------------------------------------------------
# Error constructors
# ---------------------------------------------------------------------------


def test_extractor_error_from_issues_builds_the_default_envelope() -> None:
    issues = [
        ValidationIssue(("query", "page"), "int_parsing", "bad page"),
        ValidationIssue(("query", "size"), "less_than_equal", "too big", ctx={"le": 100}),
    ]

    err = ExtractorError.from_issues(issues)

    assert err.status_code == 422
    assert err.message == "2 validation errors"
    assert err.issues == tuple(issues)
    assert err.detail == {
        "field": "page",
        "errors": ["query.page: bad page", "query.size: too big"],
        "issues": [
            {"loc": ["query", "page"], "type": "int_parsing", "msg": "bad page"},
            {"loc": ["query", "size"], "type": "less_than_equal", "msg": "too big", "ctx": {"le": 100}},
        ],
    }


def test_single_issue_message_is_the_issue_text() -> None:
    err = ExtractorFieldError.from_issues([ValidationIssue(("body", "x"), "missing", "Field required")])

    assert err.code == "extractor_field_error"
    assert err.message == "body.x: Field required"
    assert err.detail["field"] == "body"


def test_from_issues_accepts_mappings_and_extra_detail() -> None:
    err = ExtractorError.from_issues(
        [{"loc": ("item", 0), "type": "t", "msg": "m", "input": 1}],
        field="audio",
        detail={"format": "ndjson"},
    )

    assert err.detail == {
        "field": "audio",
        "errors": ["item.0: m"],
        "issues": [{"loc": ["item", 0], "type": "t", "msg": "m"}],
        "format": "ndjson",
    }


def test_websocket_validation_error_from_issues() -> None:
    err = WebSocketValidationError.from_issues(
        [ValidationIssue(("frame", "data", "age"), "int_type", "m")], event="chat.send"
    )

    assert err.code == "websocket_validation_error"
    assert err.detail == {
        "event": "chat.send",
        "issues": [{"loc": ["frame", "data", "age"], "type": "int_type", "msg": "m"}],
        "errors": ["frame.data.age: m"],
    }


# ---------------------------------------------------------------------------
# pydantic translation
# ---------------------------------------------------------------------------


def test_pydantic_errors_keep_type_loc_msg_ctx_and_drop_url() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Pet(pydantic.BaseModel):
        age: int = pydantic.Field(le=10)
        name: str

    with pytest.raises(pydantic.ValidationError) as info:
        Pet.model_validate({"age": 11})

    issues = translate_pydantic(info.value)

    assert [i.to_dict() for i in issues] == [
        {
            "loc": ["age"],
            "type": "less_than_equal",
            "msg": "Input should be less than or equal to 10",
            "ctx": {"le": 10},
        },
        {"loc": ["name"], "type": "missing", "msg": "Field required"},
    ]
    assert all("url" not in i.to_dict(include_input=True) for i in issues)


def test_pydantic_ctx_is_made_json_safe() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Model(pydantic.BaseModel):
        x: int

        @pydantic.field_validator("x")
        @classmethod
        def check(cls, value: int) -> int:
            raise ValueError("no")

    with pytest.raises(pydantic.ValidationError) as info:
        Model.model_validate({"x": 1})

    (issue,) = translate_pydantic(info.value)

    assert issue.type == "value_error"
    assert issue.ctx == {"error": "no"}


# ---------------------------------------------------------------------------
# msgspec translation (pure message parsing — msgspec need not be installed)
# ---------------------------------------------------------------------------

MSGSPEC_MESSAGES = [
    ("Object missing required field `a`", ("a",), "missing", None),
    ("Object missing required field `b` - at `$.x[0]`", ("x", 0, "b"), "missing", None),
    ("Object contains unknown field `zz`", ("zz",), "extra_forbidden", None),
    ("Expected `int`, got `str` - at `$.a`", ("a",), "int_type", None),
    ("Expected `int`, got `float` - at `$.a`", ("a",), "int_from_float", None),
    ("Expected `str`, got `int` - at `$.f[...]`", ("f", "..."), "string_type", None),
    ("Expected `array`, got `object`", (), "list_type", None),
    ("Expected `object`, got `array`", (), "dict_type", None),
    ("Expected `int` >= 0 - at `$.b`", ("b",), "greater_than_equal", {"ge": 0}),
    ("Expected `float` < 1.5", (), "less_than", {"lt": 1.5}),
    ("Expected `str` of length >= 2 - at `$.g`", ("g",), "string_too_short", {"min_length": 2}),
    ("Expected `bytes` of length <= 4", (), "bytes_too_long", {"max_length": 4}),
    ("Expected `array` of length >= 1", (), "too_short", {"field_type": "List", "min_length": 1}),
    ("Expected `int` that's a multiple of 3", (), "multiple_of", {"multiple_of": 3}),
    ("Expected `str` matching regex '^x' - at `$.g`", ("g",), "string_pattern_mismatch", {"pattern": "^x"}),
    ("Invalid enum value 'z' - at `$.e`", ("e",), "enum", {"expected": "one of the declared values"}),
    ("Invalid UUID - at `$.u`", ("u",), "uuid_parsing", None),
    ("Invalid RFC3339 encoded datetime - at `$.dt`", ("dt",), "datetime_parsing", None),
    ("Expected `MyType`, got `str`", (), "value_error", {"error": "Expected `MyType`, got `str`"}),
    ("Something unexpected", (), "value_error", {"error": "Something unexpected"}),
]


@pytest.mark.parametrize(("message", "loc", "issue_type", "ctx"), MSGSPEC_MESSAGES)
def test_msgspec_messages_translate_to_issues(
    message: str, loc: tuple[Any, ...], issue_type: str, ctx: dict[str, Any] | None
) -> None:
    (issue,) = translate_msgspec(message)

    assert issue.loc == loc
    assert issue.type == issue_type
    assert (dict(issue.ctx) if issue.ctx else None) == ctx


def test_msgspec_paths() -> None:
    assert msgspec_loc("$.a[0].b") == ("a", 0, "b")
    assert msgspec_loc("$[2]") == (2,)
    assert msgspec_loc("$") == ()


def test_msgspec_real_errors_translate() -> None:
    msgspec = pytest.importorskip("msgspec")

    class S(msgspec.Struct):
        a: int

    with pytest.raises(msgspec.ValidationError) as info:
        msgspec.convert({"a": "x"}, S)

    assert [i.to_dict() for i in translate_msgspec(str(info.value))] == [
        {"loc": ["a"], "type": "int_type", "msg": "Input should be a valid integer"}
    ]


# ---------------------------------------------------------------------------
# loc roots and synthesized issues
# ---------------------------------------------------------------------------


def _ext(source: str, name: str = "p", alias: str | None = None) -> Extraction:
    fd = FieldDescriptor(alias=alias) if alias else None
    return Extraction(
        name=name, source=source, inner_type=int, field_descriptor=fd, default=None, has_default=False
    )


@pytest.mark.parametrize(
    ("ext", "root"),
    [
        (_ext("path", "user_id"), ("path", "user_id")),
        (_ext("query", "page"), ("query", "page")),
        (_ext("query", "page", alias="p"), ("query", "p")),
        (_ext("header", "x_trace"), ("header", "x-trace")),
        (_ext("header", "x_trace", alias="X-Id"), ("header", "X-Id")),
        (_ext("cookie", "sid"), ("cookie", "sid")),
        (_ext("json", "body"), ("body",)),
        (_ext("stream", "audio"), ("body",)),
        (_ext("form", "data"), ("form",)),
        (_ext("upload_file", "avatar"), ("form", "avatar")),
        (_ext("current_user", "user"), ("current_user", "user")),
    ],
)
def test_loc_roots_per_source(ext: Extraction, root: tuple[Any, ...]) -> None:
    assert param_loc_root(ext) == root


def test_synthesized_issue_for_a_missing_value() -> None:
    err = ExtractorFieldError("missing query parameter 'q'", detail={"field": "q"})

    assert [i.to_dict() for i in synthesize_issues(err, ("query", "q"))] == [
        {"loc": ["query", "q"], "type": "missing", "msg": "Field required"}
    ]


def test_synthesized_issue_for_a_custom_failure() -> None:
    err = ExtractorError("pipe lookup failed: no such user")

    (issue,) = synthesize_issues(err, ("path", "id"))

    assert (issue.loc, issue.type, issue.msg) == (
        ("path", "id"),
        "value_error",
        "pipe lookup failed: no such user",
    )


def test_synthesized_issues_reuse_structured_error_dicts() -> None:
    err = ExtractorError(
        "x", detail={"errors": [{"loc": ("url",), "type": "missing", "msg": "Field required"}]}
    )

    assert [i.loc for i in synthesize_issues(err, ("body",))] == [("body", "url")]


def test_synthesized_issues_translate_a_pydantic_cause() -> None:
    pydantic = pytest.importorskip("pydantic")

    class M(pydantic.BaseModel):
        x: int

    try:
        try:
            M.model_validate({"x": "a"})
        except pydantic.ValidationError as exc:
            raise ExtractorError("failed") from exc
    except ExtractorError as err:
        issues = synthesize_issues(err, ("body",))

    assert [(i.loc, i.type) for i in issues] == [(("body", "x"), "int_parsing")]
