"""Startup form contracts and transport coercion regressions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, NotRequired, TypedDict

import pytest

from lauren._forms._plan import build_form_plan
from lauren._schema._compiler import TypeCompiler
from lauren._schema._params import ValidationRuntime, build_param_plan
from lauren.exceptions import DecoratorUsageError, ExtractorError, UnresolvableParameterError
from lauren.extractors import Extraction, FormField, UploadField, UploadFile
from lauren.types import FormData


def _runtime() -> ValidationRuntime:
    return ValidationRuntime(TypeCompiler(), include_input=False)


def _ext(
    tp: object, *, source: str = "form", name: str = "value", fd=None, default: object = ...
) -> Extraction:
    return Extraction(name, source, tp, fd, default, default is not ...)


def _plan(*exts: Extraction):
    return build_form_plan(exts, _runtime(), owner="Forms", handler="submit", route="POST /")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_size": 0},
        {"max_size": True},
        {"max_files": -1},
        {"min_files": -1},
        {"min_files": 2, "max_files": 1},
        {"content_types": "image/png"},
        {"content_types": ["not-media"]},
        {"alias": ""},
        {"sniff": 1},
    ],
)
def test_upload_arguments_fail_immediately(kwargs):
    with pytest.raises(DecoratorUsageError):
        UploadField(**kwargs)


def test_async_sniffer_fails_immediately():
    async def sniff(data: bytes) -> str:
        return "image/png"

    with pytest.raises(DecoratorUsageError):
        UploadField(sniff=sniff)


@pytest.mark.parametrize(
    ("tp", "fd", "reason"),
    [
        (str, UploadField(), "upload_field_placement"),
        (UploadFile, UploadField(max_files=2), "upload_count_requires_list"),
        (UploadFile, UploadField(sniff=True, content_types=["image/*"]), "unsupported_sniff_type"),
    ],
)
def test_upload_placement_errors_have_startup_context(tp, fd, reason):
    with pytest.raises(UnresolvableParameterError) as caught:
        _plan(_ext(tp, fd=fd))

    assert caught.value.detail == {
        "class": "Forms",
        "handler": "submit",
        "param": "value",
        "reason": reason,
        "field": "value",
    }


@pytest.mark.parametrize("container", [list, dict])
def test_json_files_are_rejected_before_warn_fallback(container):
    @dataclass
    class Inner:
        file: UploadFile

    tp = container[Inner] if container is list else dict[str, Inner]
    ext = _ext(tp, source="json")
    with pytest.raises(UnresolvableParameterError) as caught:
        build_param_plan(ext, tp, _runtime(), route="POST /", owner="Forms", handler="submit")

    assert caught.value.detail["reason"] == "upload_requires_form"
    assert caught.value.detail["param"] == "value"


def test_recursive_model_walker_terminates():
    @dataclass
    class Recursive:
        child: Recursive | None = None

    ext = _ext(Recursive, source="json")
    assert build_param_plan(ext, Recursive, _runtime(), route="/", owner="Forms", handler="submit")


def test_part_kind_collision_fails_startup():
    text = _ext(str, name="title", fd=FormField(alias="same"))
    file = _ext(UploadFile, source="upload_file", name="file", fd=UploadField(alias="same"))

    with pytest.raises(UnresolvableParameterError) as caught:
        _plan(text, file)

    assert caught.value.detail["reason"] == "form_kind_collision"


def test_repeated_upload_alias_constraints_accumulate_and_are_snapshots():
    descriptor = UploadField(alias="files", max_size=7, content_types=["image/png"])
    plan = _plan(
        _ext(UploadFile, name="a", fd=descriptor),
        _ext(UploadFile, name="b", fd=UploadField(alias="files", max_size=3)),
    )
    descriptor.max_size = 100
    descriptor.content_types = ["text/plain"]

    assert [spec.max_size for spec in plan.rules["files"].uploads] == [7, 3]
    assert plan.rules["files"].uploads[0].content_types == ("image/png",)
    with pytest.raises(TypeError):
        plan.rules["other"] = plan.rules["files"]


@pytest.mark.parametrize(
    ("tp", "values", "expected"),
    [
        (str, ["", "second"], ""),
        (int, ["12", "99"], 12),
        (list[int], ["1", "", "2"], [1, 2]),
        (list[str], ["", "a"], ["", "a"]),
        (bool, ["on"], True),
        (bool, ["yes"], True),
        (bool, ["off"], False),
        (bool, ["0"], False),
    ],
)
def test_precompiled_coercers_follow_form_transport_semantics(tp, values, expected):
    plan = _plan(_ext(tp))
    field = plan.bindings[0].fields[0]

    assert field.collect(FormData(("value", value) for value in values)) == expected


def test_model_aliases_and_required_nullable_schema():
    class FormModel(TypedDict):
        count: Annotated[int | None, FormField(alias="item-count")]
        enabled: NotRequired[bool]

    plan = _plan(_ext(FormModel))

    assert set(plan.rules) == {"item-count", "enabled"}
    assert plan.schema()["required"] == ["item-count"]


@pytest.mark.asyncio
async def test_form_model_is_always_enforced_and_unknown_text_reaches_backend(monkeypatch):
    from lauren._forms import _runtime as form_runtime
    from lauren.validation import validation_config

    @validation_config(extra="forbid")
    @dataclass
    class Model:
        count: int

    async def read(_request):
        return FormData([("count", "3"), ("unexpected", "x")])

    monkeypatch.setattr(form_runtime, "read_form_data", read)
    ext = _ext(Model)
    _plan(ext)
    with pytest.raises(ExtractorError) as caught:
        await ext.compiled.extract(object(), ext)

    assert caught.value.issues[0].loc == ("form", "unexpected")
    assert caught.value.issues[0].type == "extra_forbidden"


@pytest.mark.asyncio
async def test_blank_nonstring_uses_default_but_required_nullable_stays_required(monkeypatch):
    from lauren._forms import _runtime as form_runtime

    @dataclass
    class Model:
        required: int | None
        defaulted: int = 7

    async def read(_request):
        return FormData([("required", ""), ("defaulted", "")])

    monkeypatch.setattr(form_runtime, "read_form_data", read)
    ext = _ext(Model)
    _plan(ext)
    with pytest.raises(ExtractorError) as caught:
        await ext.compiled.extract(object(), ext)

    assert [(issue.loc, issue.type) for issue in caught.value.issues] == [(("form", "required"), "missing")]


@pytest.mark.asyncio
async def test_nullable_field_alias_inside_union_is_used_by_final_validator(monkeypatch):
    from lauren._forms import _runtime as form_runtime

    @dataclass
    class Model:
        count: Annotated[int, FormField(alias="item-count")] | None = None

    async def read(_request):
        return FormData([("item-count", "3")])

    monkeypatch.setattr(form_runtime, "read_form_data", read)
    ext = _ext(Model)
    _plan(ext)

    assert (await ext.compiled.extract(object(), ext)).count == 3


@pytest.mark.asyncio
async def test_nullable_model_is_required_unless_a_default_is_declared(monkeypatch):
    from lauren._forms import _runtime as form_runtime

    @dataclass
    class Model:
        count: int

    async def read(_request):
        return FormData([])

    monkeypatch.setattr(form_runtime, "read_form_data", read)
    required = _ext(Model | None)
    optional = _ext(Model | None, default=None)
    _plan(required, optional)

    with pytest.raises(ExtractorError):
        await required.compiled.extract(object(), required)
    assert await optional.compiled.extract(object(), optional) is None


def test_top_level_form_markers_are_preserved_for_route_planning():
    from lauren.extractors import Form
    from lauren.validation import Ge

    runtime = _runtime()
    ext = _ext(int)
    build_param_plan(ext, Form[int, Ge(3)], runtime, route="/", owner="Forms", handler="submit")
    plan = build_form_plan([ext], runtime)

    assert plan.schema()["properties"]["value"]["minimum"] == 3


def test_nested_upload_field_shape_is_rejected():
    @dataclass
    class Model:
        nested: dict[str, UploadFile]

    with pytest.raises(UnresolvableParameterError) as caught:
        _plan(_ext(Model))

    assert caught.value.detail["reason"] == "nested_upload"


def test_raw_formdata_is_the_only_untyped_binding_that_captures_unknown_files():
    plan = _plan(_ext(FormData))
    raw_dict = _plan(_ext(dict[str, list[str]]))

    assert plan.takes_all is True
    assert raw_dict.takes_all is False
    assert plan.legacy_urlencoded is False
    assert raw_dict.legacy_urlencoded is True
    assert _plan(_ext(dict), _ext(str, name="typed")).legacy_urlencoded is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("unknown", "undeclared_part"),
        ("kind", "expected_file"),
        ("count", "too_few_files"),
    ],
)
async def test_early_cached_parse_rechecks_complete_route_rules(case, reason):
    from types import SimpleNamespace

    from lauren._forms._config import MultipartConfig
    from lauren.types import Request

    plan = _plan(_ext(list[UploadFile], source="upload_file", fd=UploadField(min_files=1)))
    request = Request(method="POST", path="/")
    request.__dict__["__lauren_content__"] = SimpleNamespace(
        multipart=MultipartConfig(undeclared_parts="reject")
    )
    form = FormData(
        [("extra", "x")] if case == "unknown" else [("value", "not a file")] if case == "kind" else []
    )

    with pytest.raises(ExtractorError) as caught:
        await plan.validate_cached(request, form)
    with pytest.raises(ExtractorError) as repeated:
        await plan.validate_cached(request, form)

    assert caught.value.detail["reason"] == reason
    assert repeated.value is caught.value


def test_bare_form_shorthand_compiles_as_a_legacy_raw_dict():
    from lauren.extractors import Form, parse_extractor_hint

    source, inner, *_ = parse_extractor_hint(Form)
    plan = _plan(_ext(inner, source=source))

    assert inner is dict
    assert plan.legacy_urlencoded is True
    assert plan.bindings[0].mode == "dict"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["dataclass", "typeddict", "pydantic", "msgspec"])
async def test_missing_coercion_and_forbidden_extra_errors_aggregate_without_model_validation(
    monkeypatch, backend
):
    from lauren._forms import _runtime as form_runtime
    from lauren.validation import validation_config

    calls = []
    if backend == "dataclass":

        @validation_config(extra="forbid")
        @dataclass
        class Search:
            q: str
            page: Annotated[int, FormField(alias="pageSize")] = 10

            def __post_init__(self):
                calls.append(self)
    elif backend == "typeddict":

        @validation_config(extra="forbid")
        class Search(TypedDict):
            q: str
            page: NotRequired[Annotated[int, FormField(alias="pageSize")]]
    elif backend == "pydantic":
        pydantic = pytest.importorskip("pydantic")

        class Search(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")
            q: str
            page: int = pydantic.Field(default=10, alias="pageSize")

            @pydantic.model_validator(mode="before")
            @classmethod
            def observe(cls, data):
                calls.append(data)
                return data
    else:
        msgspec = pytest.importorskip("msgspec")
        Search = msgspec.defstruct(
            "Search", [("q", str), ("page", int, 10)], rename={"page": "pageSize"}, forbid_unknown_fields=True
        )

    async def read(_request):
        return FormData([("pageSize", "bad"), ("unexpected", "x")])

    monkeypatch.setattr(form_runtime, "read_form_data", read)
    ext = _ext(Search)
    _plan(ext)
    with pytest.raises(ExtractorError) as caught:
        await ext.compiled.extract(object(), ext)

    assert [(issue.loc, issue.type) for issue in caught.value.issues] == [
        (("form", "q"), "missing"),
        (("form", "pageSize"), "int_parsing"),
        (("form", "unexpected"), "extra_forbidden"),
    ]
    assert calls == []


@pytest.mark.parametrize("composed", [False, True])
def test_explicit_descriptor_merges_only_an_implicit_legacy_optional_default(composed):
    from lauren.extractors import Query, QueryField, parse_extractor_hint, pipe

    descriptor = QueryField(alias="page", ge=1)
    metadata = descriptor | pipe(lambda value: value) if composed else descriptor
    annotation = Annotated[Query[int] | None, metadata]

    source, inner, _, _, fd, pipes = parse_extractor_hint(annotation)

    assert source == "query"
    assert inner is int
    assert fd.default is None and fd.alias == "page" and fd.ge == 1
    assert len(pipes) == int(composed)
    assert descriptor.default is ...  # Never mutate the declaration.


@pytest.mark.parametrize("source", ["query", "form", "upload"])
def test_nullable_annotations_still_reject_two_real_field_descriptors(source):
    from lauren.extractors import Form, Query, QueryField, parse_extractor_hint

    if source == "query":
        annotation = Annotated[Query[int, QueryField(ge=1)] | None, QueryField(le=9)]
    elif source == "form":
        annotation = Annotated[Form[int, FormField(ge=1)] | None, FormField(le=9)]
    else:
        annotation = Annotated[
            Annotated[UploadFile, UploadField(max_size=10)] | None, UploadField(content_types=["image/png"])
        ]

    with pytest.raises(ExtractorError, match="multiple FieldDescriptor"):
        parse_extractor_hint(annotation)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["form-inner", "form-outer", "form-alias", "upload", "upload-alias"])
@pytest.mark.parametrize("default_kind", ["absent", "function", "descriptor"])
async def test_nullable_standalone_forms_require_an_explicit_default(monkeypatch, shape, default_kind):
    from lauren._forms import _runtime as form_runtime
    from lauren.extractors import Form, parse_extractor_hint

    descriptor = UploadField(alias="part") if shape.startswith("upload") else FormField(alias="part")
    if default_kind == "descriptor":
        descriptor.default = None
    annotation = {
        "form-inner": Form[int | None],
        "form-outer": Form[int] | None,
        "form-alias": Annotated[Form[int] | None, descriptor],
        "upload": UploadFile | None,
        "upload-alias": Annotated[UploadFile | None, descriptor],
    }[shape]
    if default_kind == "descriptor" and shape not in ("form-alias", "upload-alias"):
        annotation = Annotated[annotation, descriptor]
    source, inner, _, _, fd, _ = parse_extractor_hint(annotation)
    default = None if default_kind == "function" else fd.default if fd is not None else ...
    ext = _ext(inner, source=source, fd=fd, default=default)
    _plan(ext)

    async def read(_request):
        return FormData([])

    monkeypatch.setattr(form_runtime, "read_form_data", read)
    if default_kind == "absent":
        assert fd is None or fd.default is ...
        with pytest.raises(ExtractorError) as caught:
            await ext.compiled.extract(object(), ext)
        assert caught.value.issues[0].type == "missing"
    else:
        assert await ext.compiled.extract(object(), ext) is None
