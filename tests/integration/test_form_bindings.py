"""Full-stack form bindings retain backend validation and flat wire semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, NotRequired, TypedDict

import pytest

from lauren import Form, LaurenFactory, controller, middleware, module, post
from lauren.types import CallNext, Request, Response
from lauren.exceptions import UnresolvableParameterError
from lauren.extractors import FormField, Json, UploadField, UploadFile
from lauren.testing import TestClient
from lauren.validation import validation_config


@dataclass
class DataclassForm:
    title: Annotated[str, FormField(alias="display-name")]
    age: int
    active: bool = False
    tags: list[int] = field(default_factory=list)


class TypedDictForm(TypedDict):
    title: Annotated[str, FormField(alias="display-name")]
    age: int
    active: NotRequired[bool]
    tags: NotRequired[list[int]]


def _model(backend: str, *, files: bool = False):
    if backend == "dataclass":
        if not files:
            return DataclassForm

        @dataclass
        class FilesForm:
            title: str
            files: Annotated[list[UploadFile], UploadField(max_files=2)] = field(default_factory=list)

        return FilesForm
    if backend == "typeddict":
        if not files:
            return TypedDictForm

        class FilesDict(TypedDict):
            title: str
            files: list[UploadFile]

        return FilesDict
    if backend == "pydantic":
        pydantic = pytest.importorskip("pydantic")
        if files:
            return pydantic.create_model("FilesPydantic", title=(str, ...), files=(list[UploadFile], ...))
        return pydantic.create_model(
            "FieldsPydantic",
            title=(str, pydantic.Field(alias="display-name")),
            age=(int, ...),
            active=(bool, False),
            tags=(list[int], pydantic.Field(default_factory=list)),
        )
    msgspec = pytest.importorskip("msgspec")
    if files:
        return msgspec.defstruct("FilesMsgspec", [("title", str), ("files", list[UploadFile])])
    return msgspec.defstruct(
        "FieldsMsgspec",
        [
            ("title", str),
            ("age", int),
            ("active", bool, False),
            ("tags", list[int], msgspec.field(default_factory=list)),
        ],
        rename={"title": "display-name"},
    )


def _client_for(model, *, source=Form, files: bool = False, options=None) -> TestClient:
    async def submit(self, form):
        def value(name):
            return form[name] if isinstance(form, dict) else getattr(form, name)

        if files:
            uploads = value("files")
            return {
                "title": value("title"),
                "files": [f.filename for f in uploads],
                "public": all(isinstance(f, UploadFile) for f in uploads),
                "content": [(await f.read()).decode() for f in uploads],
            }
        return {name: value(name) for name in ("title", "age", "active", "tags")}

    submit.__annotations__ = {"form": source[model], "return": dict}
    cls = controller("/bindings")(type("Bindings", (), {"submit": post("/")(submit)}))
    root = module(controllers=[cls])(type("BindingsModule", (), {}))
    return TestClient(LaurenFactory.create(root, **(options or {})))


@pytest.mark.parametrize("backend", ["dataclass", "typeddict", "pydantic", "msgspec"])
@pytest.mark.parametrize("multipart", [False, True])
def test_typed_form_backends_share_aliases_coercion_and_repeated_fields(backend, multipart):
    model = _model(backend)
    data = [
        ("display-name", "Ada"),
        ("age", "3"),
        ("age", "99"),
        ("active", "on"),
        ("tags", "1"),
        ("tags", "2"),
    ]
    kwargs = {"data": data}
    if multipart:
        kwargs["files"] = {"ignored": ("ignored.txt", b"ignored")}
    with _client_for(model) as client:
        response = client.post("/bindings", **kwargs)

    assert response.status_code == 200, response.text
    assert response.json() == {"title": "Ada", "age": 3, "active": True, "tags": [1, 2]}


@pytest.mark.parametrize("backend", ["dataclass", "typeddict", "pydantic", "msgspec"])
def test_form_models_keep_public_upload_instances_for_every_backend(backend):
    model = _model(backend, files=True)
    with _client_for(model, files=True) as client:
        response = client.post(
            "/bindings",
            data={"title": "Docs"},
            files=[("files", ("a.txt", b"a")), ("files", ("b.txt", b"b"))],
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "title": "Docs",
        "files": ["a.txt", "b.txt"],
        "public": True,
        "content": ["a", "b"],
    }


@pytest.mark.parametrize("backend", ["dataclass", "typeddict", "pydantic", "msgspec"])
def test_json_models_with_files_fail_at_startup_for_every_backend(backend):
    model = _model(backend, files=True)
    with pytest.raises(UnresolvableParameterError) as caught:
        _client_for(model, source=Json)

    assert caught.value.detail["param"] == "form"
    assert caught.value.detail["reason"] == "upload_requires_form"


@pytest.mark.parametrize("backend", ["dataclass", "typeddict", "pydantic", "msgspec"])
def test_invalid_checkbox_is_never_silently_false(backend):
    with _client_for(_model(backend)) as client:
        response = client.post("/bindings", data={"display-name": "Ada", "age": "3", "active": "garbage"})

    assert response.status_code == 422
    assert response.json()["error"]["detail"]["issues"][0]["loc"] == ["form", "active"]


def test_pydantic_final_model_validator_still_runs():
    pydantic = pytest.importorskip("pydantic")

    class Model(pydantic.BaseModel):
        title: str = pydantic.Field(alias="display-name")
        age: int
        active: bool = False
        tags: list[int] = pydantic.Field(default_factory=list)

        @pydantic.model_validator(mode="after")
        def check_age(self):
            if self.age < 18:
                raise ValueError("must be an adult")
            return self

    with _client_for(Model) as client:
        response = client.post("/bindings", data={"display-name": "Ada", "age": "3"})

    assert response.status_code == 422
    assert "must be an adult" in response.text


def test_stdlib_extra_forbid_receives_unknown_text_keys():
    @validation_config(extra="forbid")
    @dataclass
    class Model:
        title: str
        age: int
        active: bool = False
        tags: list[int] = field(default_factory=list)

    with _client_for(Model) as client:
        response = client.post("/bindings", data={"title": "Ada", "age": "3", "extra": "x"})

    assert response.status_code == 422
    issue = response.json()["error"]["detail"]["issues"][0]
    assert issue["type"] == "extra_forbidden"
    assert issue["loc"] == ["form", "extra"]


def test_bound_form_requests_do_not_reflect_model_fields(monkeypatch):
    with _client_for(DataclassForm) as client:

        def forbidden(*_args, **_kwargs):
            raise AssertionError("request-time model reflection")

        monkeypatch.setattr("lauren._di._safe_class_hints", forbidden)
        monkeypatch.setattr("lauren._forms._plan._fields", forbidden)
        monkeypatch.setattr("lauren._typing.resolve_type_hints", forbidden)
        monkeypatch.setattr("lauren.extractors._convert_struct", forbidden)
        response = client.post("/bindings", data={"display-name": "Ada", "age": "3"})

    assert response.status_code == 200, response.text


def _upload_client(descriptor, *, early: bool, content=None):
    from lauren.types import FormData

    @middleware()
    class ParseEarly:
        async def dispatch(self, request: Request, call_next: CallNext) -> Response:
            form = await request.multipart()
            upload = form.get("file")
            if upload is not None:
                await upload.seek(2)
            return await call_next(request)

    async def submit(self, raw, file):
        return {"content": (await file.read()).hex()}

    submit.__annotations__ = {
        "raw": Form[FormData],
        "file": Annotated[UploadFile, descriptor],
        "return": dict,
    }
    cls = controller("/cached")(type("CachedUpload", (), {"submit": post("/")(submit)}))
    root = module(controllers=[cls])(type("CachedUploadModule", (), {}))
    return TestClient(
        LaurenFactory.create(root, global_middlewares=[ParseEarly] if early else [], content=content)
    )


@pytest.mark.parametrize(
    ("descriptor", "body", "media", "status", "reason"),
    [
        (UploadField(max_size=2), b"abcd", "text/plain", 413, "file_too_large"),
        (UploadField(content_types=["image/png"]), b"abcd", "text/plain", 422, "content_type_not_allowed"),
        (
            UploadField(content_types=["image/png"], sniff=True),
            b"not a PNG",
            "image/png",
            422,
            "content_mismatch",
        ),
    ],
)
def test_pre_routing_parse_cannot_bypass_upload_route_constraints(descriptor, body, media, status, reason):
    with _upload_client(descriptor, early=True) as client:
        response = client.post("/cached", files={"file": ("a.bin", body, media)})

    assert response.status_code == status, response.text
    assert response.json()["error"]["detail"]["reason"] == reason


@pytest.mark.parametrize("early", [False, True])
def test_custom_sniff_runs_once_and_cached_revalidation_preserves_cursor(early):
    calls = []

    def sniff(prefix):
        calls.append(prefix)
        return "image/png"

    payload = b"\x89PNG\r\n\x1a\nrest"
    with _upload_client(UploadField(content_types=["image/png"], sniff=sniff), early=early) as client:
        response = client.post("/cached", files={"file": ("a.png", payload, "image/png")})

    assert response.status_code == 200, response.text
    assert response.json() == {"content": (payload[2:] if early else payload).hex()}
    assert calls == [payload]
