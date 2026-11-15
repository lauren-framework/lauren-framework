"""Forms share one multipart parse and return the types their annotations promise."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

import pytest

from lauren import (
    Form,
    FormData,
    LaurenFactory,
    MultipartConfig,
    Request,
    UploadField,
    UploadFile,
    controller,
    module,
    post,
)
from lauren.testing import TestClient
from lauren.types import ContentConfig


@dataclass
class Details:
    title: str
    age: int
    active: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass
class FileDetails:
    title: str
    attachments: list[UploadFile] = field(default_factory=list)


@controller("/forms")
class Forms:
    @post("/details")
    async def details(self, form: Form[Details]) -> dict:
        return {"title": form.title, "age": form.age, "active": form.active, "tags": form.tags}

    @post("/mixed")
    async def mixed(
        self,
        name: Form[str],
        avatar: Annotated[UploadFile, UploadField(max_size=1024)] | None = None,
    ) -> dict:
        return {
            "name": name,
            "filename": avatar.filename if avatar else None,
            "content": (await avatar.read()).decode() if avatar else None,
        }

    @post("/files")
    async def files(self, form: Form[FileDetails]) -> dict:
        return {"title": form.title, "files": [f.filename for f in form.attachments]}

    @post("/raw")
    async def raw(self, request: Request, form: Form[FormData]) -> dict:
        return {"same": await request.multipart() is form, "title": form.getall("title")}


@module(controllers=[Forms])
class AppModule:
    pass


def _client() -> TestClient:
    return TestClient(
        LaurenFactory.create(
            AppModule,
            content=ContentConfig(multipart=MultipartConfig(spool_threshold=16)),
        )
    )


def test_urlencoded_model_coerces_scalars_and_repeated_values() -> None:
    with _client() as client:
        response = client.post(
            "/forms/details",
            data=[("title", "Ada"), ("age", "3"), ("active", "on"), ("tags", "a"), ("tags", "b")],
        )
    assert response.status_code == 200
    assert response.json() == {"title": "Ada", "age": 3, "active": True, "tags": ["a", "b"]}


def test_multipart_text_and_optional_file_share_the_body() -> None:
    with _client() as client:
        response = client.post(
            "/forms/mixed", data={"name": "Ada"}, files={"avatar": ("a.txt", b"abc", "text/plain")}
        )
    assert response.status_code == 200
    assert response.json() == {"name": "Ada", "filename": "a.txt", "content": "abc"}


def test_files_are_fields_of_the_form_model() -> None:
    with _client() as client:
        response = client.post(
            "/forms/files",
            data={"title": "documents"},
            files=[("attachments", ("a.txt", b"a")), ("attachments", ("b.txt", b"b"))],
        )
    assert response.status_code == 200
    assert response.json() == {"title": "documents", "files": ["a.txt", "b.txt"]}


def test_imperative_multipart_accessor_reuses_the_extracted_form_data() -> None:
    with _client() as client:
        response = client.post(
            "/forms/raw", data=[("title", "a"), ("title", "b")], files={"file": ("a.txt", b"a")}
        )
    assert response.status_code == 200
    assert response.json() == {"same": True, "title": ["a", "b"]}


def test_openapi_merges_form_parameters_and_models_into_binary_object_schemas() -> None:
    app = LaurenFactory.create(AppModule)
    paths = app.openapi()["paths"]
    files = paths["/forms/files"]["post"]["requestBody"]["content"]
    assert set(files) == {"multipart/form-data"}
    attachment = files["multipart/form-data"]["schema"]["properties"]["attachments"]
    assert attachment["type"] == "array"
    assert attachment["items"] == {"type": "string", "format": "binary"}
    mixed = paths["/forms/mixed"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert set(mixed["properties"]) == {"name", "avatar"}
    text = paths["/forms/details"]["post"]["requestBody"]["content"]
    assert set(text) == {"application/x-www-form-urlencoded", "multipart/form-data"}


def test_upload_lives_through_background_tasks_then_closes() -> None:
    from lauren import BackgroundTasks

    retained = []
    processed = []

    @controller("/owned")
    class Owned:
        @post("/")
        async def upload(self, file: UploadFile, tasks: BackgroundTasks) -> dict:
            retained.append(file)

            async def process():
                processed.append(await file.read())

            tasks.add_task(process)
            return {"size": file.size}

    @module(controllers=[Owned])
    class Root:
        pass

    with TestClient(
        LaurenFactory.create(Root, content=ContentConfig(multipart=MultipartConfig(spool_threshold=2)))
    ) as client:
        response = client.post("/owned", files={"file": ("a.txt", b"abc")})
    assert response.status_code == 200
    assert processed == [b"abc"]
    assert retained[0].file.closed


def test_upload_lives_through_streaming_response_then_closes() -> None:
    from lauren import EventStream

    retained = []
    states = []

    @controller("/owned-stream")
    class Owned:
        @post("/")
        async def upload(self, file: UploadFile) -> EventStream:
            retained.append(file)

            async def events():
                async for chunk in file.stream(1):
                    states.append(file.file.closed)
                    yield chunk

            return EventStream(events())

    @module(controllers=[Owned])
    class Root:
        pass

    with TestClient(LaurenFactory.create(Root)) as client:
        response = client.post("/owned-stream", files={"file": ("a.txt", b"abc")})
    assert response.status_code == 200
    assert states == [False, False, False]
    assert retained[0].file.closed


def test_handler_failure_still_closes_the_upload() -> None:
    retained = []

    @controller("/owned-error")
    class Owned:
        @post("/")
        async def upload(self, file: UploadFile) -> dict:
            retained.append(file)
            raise RuntimeError("example handler failure")

    @module(controllers=[Owned])
    class Root:
        pass

    with TestClient(LaurenFactory.create(Root), raise_server_exceptions=False) as client:
        response = client.post("/owned-error", files={"file": ("a.txt", b"abc")})
    assert response.status_code == 500
    assert retained[0].file.closed


@pytest.mark.parametrize("streamed", [False, True])
def test_explicit_raw_body_and_upload_parameters_share_buffered_replay(streamed) -> None:
    from lauren import ByteStream, Bytes

    marker = ByteStream if streamed else Bytes

    @controller("/replay")
    class Replay:
        @post("/before")
        async def before(self, raw: marker, file: UploadFile) -> dict:
            data = raw if isinstance(raw, bytes) else await raw.read_all()
            return {"raw": b"abc" in data, "file": await file.read() == b"abc"}

        @post("/after")
        async def after(self, file: UploadFile, raw: marker) -> dict:
            data = raw if isinstance(raw, bytes) else await raw.read_all()
            return {"raw": b"abc" in data, "file": await file.read() == b"abc"}

    @module(controllers=[Replay])
    class Root:
        pass

    with TestClient(LaurenFactory.create(Root)) as client:
        before = client.post("/replay/before", files={"file": ("a.txt", b"abc")})
        after = client.post("/replay/after", files={"file": ("a.txt", b"abc")})
    assert before.json() == after.json() == {"raw": True, "file": True}


@pytest.mark.parametrize("allowed", [("image/png",), ("image/png", "image/jpeg"), ("image/*",)])
def test_file_schema_documents_counts_aliases_and_concrete_media_hints(allowed) -> None:
    field_type = Annotated[
        list[UploadFile] | None,
        UploadField(
            alias="photos", description="Profile images", content_types=allowed, min_files=1, max_files=2
        ),
    ]

    @controller("/schema-uploads")
    class Uploads:
        @post("/")
        async def upload(self, files: field_type = None) -> dict:
            return {}

    @module(controllers=[Uploads])
    class Root:
        pass

    body = LaurenFactory.create(Root).openapi()["paths"]["/schema-uploads"]["post"]["requestBody"]
    media = body["content"]["multipart/form-data"]
    prop = media["schema"]["properties"]["photos"]
    array = next(item for item in prop["anyOf"] if item.get("type") == "array")
    assert array["minItems"] == 1 and array["maxItems"] == 2
    assert prop["description"] == "Profile images"
    assert media["encoding"]["photos"]["contentType"] == ", ".join(allowed)
    assert array["items"].get("contentMediaType") == ("image/png" if allowed == ("image/png",) else None)


def test_raw_form_data_also_accepts_urlencoded_text_pairs() -> None:
    @controller("/raw-text")
    class Raw:
        @post("/")
        async def raw(self, form: Form[FormData]) -> dict:
            return {"values": form.getall("x")}

    @module(controllers=[Raw])
    class Root:
        pass

    app = LaurenFactory.create(Root)
    media = app.openapi()["paths"]["/raw-text"]["post"]["requestBody"]["content"]
    with TestClient(app) as client:
        response = client.post("/raw-text", data=[("x", "a"), ("x", "b")])
    assert set(media) == {"application/x-www-form-urlencoded", "multipart/form-data"}
    assert response.json() == {"values": ["a", "b"]}
