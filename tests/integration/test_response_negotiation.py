"""Response negotiation (``produces=``), codecs on both sides and malformed bodies (F03 §5.3, §5.5, §5.6)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from lauren import (
    Bytes,
    ContentConfig,
    Created,
    Json,
    JsonCodec,
    LaurenFactory,
    Response,
    cache_response,
    controller,
    get,
    module,
    post,
    use_encoder,
)
from lauren.cache import CacheModule
from lauren.exceptions import StartupError
from lauren.reflect import get_all_routes, reflect_routes
from lauren.serialization import StdlibJSONEncoder
from lauren.testing import TestClient

JSON = "application/json"
KV = "application/x-kv"


class KvCodec:
    """A dependency-free test codec: ``key=value`` lines (values are JSON)."""

    media_types = (KV,)

    def decode(self, data: bytes) -> Any:
        out = {}
        for line in data.decode().splitlines():
            key, _, value = line.partition("=")
            out[key] = json.loads(value)
        return out

    def encode(self, value: Any) -> bytes:
        return "\n".join(f"{k}={json.dumps(v)}" for k, v in value.items()).encode()


class LoudEncoder(StdlibJSONEncoder):
    name = "loud"

    def encode_compact(self, value: Any) -> bytes:
        return super().encode_compact({"loud": value})


@dataclass
class User:
    id: int
    name: str


CALLS: list[str] = []


@controller("/users")
class Users:
    @get("/{id}", produces=[JSON, KV])
    async def show(self, id: int) -> User:
        CALLS.append("show")
        return User(id, "ada")

    @get("/untyped/{id}", produces=[JSON, KV])
    async def untyped(self, id: int):  # type: ignore[no-untyped-def]
        CALLS.append("untyped")
        return {"id": id}

    @get("/bare/{id}")
    async def bare(self, id: int):  # type: ignore[no-untyped-def]
        CALLS.append("bare")
        return {"id": id}

    @get("/text", produces=[JSON, KV])
    async def text(self) -> str:
        return "hello"

    @get("/explicit", produces=[JSON, KV])
    async def explicit(self) -> Response:
        return Response.json({"explicit": True})

    @get("/loud", produces=[JSON, KV])
    @use_encoder(LoudEncoder())
    async def loud(self) -> dict:
        return {"a": 1}

    @post("/", produces=[JSON, KV], status_code=201)
    async def create(self, body: Json[User]) -> User:
        return body

    @post("/created", produces=[KV])
    async def created(self) -> Created[User]:
        return Created(User(2, "bo"), location="/users/2")

    @post("/kv", consumes=[KV, JSON])
    async def accept_kv(self, body: Json[User]) -> dict:
        return {"name": body.name}

    @get("/plain")
    async def plain(self) -> dict:
        return {"plain": True}


@controller("/kv", produces=[KV])
class KvOnly:
    @get("/")
    async def index(self) -> dict:
        return {"kv": 1}

    @get("/json", produces=[JSON])
    async def json_route(self) -> dict:
        return {"json": 1}


@module(controllers=[Users, KvOnly])
class AppModule:
    pass


def _client(**content: Any) -> TestClient:
    content.setdefault("codecs", [KvCodec()])
    return TestClient(LaurenFactory.create(AppModule, content=ContentConfig(**content)))


@pytest.mark.parametrize(
    ("accept", "media"),
    [
        (None, JSON),
        ("*/*", JSON),
        (KV, KV),
        (f"{KV};q=0.9, {JSON};q=0.5", KV),
        (f"{JSON};q=0, */*", KV),
        ("text/html", JSON),
        ("application/*", JSON),
    ],
)
def test_accept_selects_the_representation(accept: str | None, media: str) -> None:
    headers = {"accept": accept} if accept is not None else {}

    response = _client().get("/users/1", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == media
    assert response.headers["vary"] == "Accept"
    decoded = KvCodec().decode(response.content) if media == KV else response.json()
    assert decoded == {"id": 1, "name": "ada"}


def test_routes_with_the_default_produces_do_not_vary() -> None:
    response = _client().get("/users/plain", headers={"accept": KV})

    assert response.headers["content-type"] == JSON
    assert "vary" not in response.headers


def test_strict_accept_rejects_before_a_typed_handler_runs() -> None:
    CALLS.clear()

    response = _client(strict_accept=True).get("/users/1", headers={"accept": "text/html"})

    assert response.status_code == 406
    assert response.json()["error"]["code"] == "not_acceptable"
    assert response.json()["error"]["detail"] == {"available": [JSON, KV]}
    assert CALLS == []


def test_strict_accept_with_explicit_produces_rejects_before_an_untyped_handler() -> None:
    CALLS.clear()

    response = _client(strict_accept=True).get("/users/untyped/3", headers={"accept": "text/html"})

    assert response.status_code == 406
    assert CALLS == []


def test_strict_accept_on_an_undeclared_untyped_route_decides_at_serialization() -> None:
    CALLS.clear()

    response = _client(strict_accept=True).get("/users/bare/3", headers={"accept": "text/html"})

    assert response.status_code == 406
    assert CALLS == ["bare"]


def test_strict_accept_on_default_routes_still_serves_wildcards() -> None:
    client = _client(strict_accept=True)

    assert client.get("/users/plain", headers={"accept": "*/*"}).status_code == 200
    assert client.get("/users/plain", headers={"accept": "text/html"}).status_code == 406


def test_text_is_not_negotiated() -> None:
    response = _client(strict_accept=True).get("/users/text", headers={"accept": "text/plain"})

    assert response.status_code == 200
    assert response.text == "hello"
    assert "vary" not in response.headers


def test_explicit_responses_pass_through_untouched() -> None:
    response = _client().get("/users/explicit", headers={"accept": KV})

    assert response.headers["content-type"] == JSON
    assert response.json() == {"explicit": True}
    assert "vary" not in response.headers


def test_use_encoder_still_writes_the_json_representation() -> None:
    client = _client()

    as_json = client.get("/users/loud")
    as_kv = client.get("/users/loud", headers={"accept": KV})

    assert as_json.json() == {"loud": {"a": 1}}
    assert KvCodec().decode(as_kv.content) == {"a": 1}


def test_negotiation_composes_with_declared_statuses_and_typed_results() -> None:
    client = _client()

    created = client.post("/users/", json={"id": 9, "name": "cy"}, headers={"accept": KV})
    typed = client.post("/users/created")

    assert (created.status_code, created.headers["content-type"]) == (201, KV)
    assert KvCodec().decode(created.content) == {"id": 9, "name": "cy"}
    assert (typed.status_code, typed.headers["content-type"], typed.headers["location"]) == (
        201,
        KV,
        "/users/2",
    )


def test_controller_produces_is_the_default_of_its_routes() -> None:
    client = _client()

    index = client.get("/kv/")
    override = client.get("/kv/json")

    assert index.headers["content-type"] == KV and KvCodec().decode(index.content) == {"kv": 1}
    assert override.headers["content-type"] == JSON


def test_default_produces_applies_app_wide() -> None:
    response = _client(default_produces=[KV, JSON]).get("/users/plain")

    assert response.headers["content-type"] == KV
    assert response.headers["vary"] == "Accept"


def test_a_declared_codec_decodes_the_request_body() -> None:
    client = _client()

    kv = client.post("/users/kv", content=b'id=1\nname="kay"', headers={"content-type": KV})
    js = client.post("/users/kv", json={"id": 1, "name": "jay"})

    assert kv.json() == {"name": "kay"}
    assert js.json() == {"name": "jay"}


def test_a_codec_decode_failure_is_a_malformed_body() -> None:
    response = _client().post("/users/kv", content=b"id=not-json", headers={"content-type": KV})

    error = response.json()["error"]
    assert response.status_code == 422
    assert error["detail"]["reason"] == "malformed_body"
    assert error["detail"]["issues"][0]["type"] == "body_invalid"
    assert error["message"].startswith(f"invalid {KV} body")


def test_unexpected_codec_failure_is_not_reported_as_a_client_error() -> None:
    class BrokenCodec(KvCodec):
        def decode(self, data: bytes) -> Any:
            raise RuntimeError("codec bug")

    client = _client(codecs=[BrokenCodec()])

    response = client.post("/users/kv", content=b"id=1", headers={"content-type": KV})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


@pytest.mark.parametrize(
    ("status", "code"),
    [(422, "extractor_error"), (400, "bad_request")],
)
def test_malformed_json_status_is_configurable(status: int, code: str) -> None:
    response = _client(malformed_body_status=status).post(
        "/users/", content=b'{"id": 1,\n "name": }', headers={"content-type": JSON}
    )

    error = response.json()["error"]
    assert response.status_code == status
    assert error["code"] == code
    assert error["detail"]["reason"] == "malformed_body"
    assert error["detail"]["issues"][0]["type"] == "json_invalid"
    if status == 400:
        assert (error["detail"]["line"], error["detail"]["col"]) == (2, 10)


def test_schema_errors_stay_422_with_malformed_body_status_400() -> None:
    response = _client(malformed_body_status=400).post("/users/", json=[1, 2])

    assert response.status_code == 422


def test_a_response_cache_keys_on_the_negotiated_representation() -> None:
    @controller("/cached")
    class Cached:
        @get("/", produces=[JSON, KV])
        @cache_response(ttl=60, debug_header=True)
        async def index(self) -> dict:
            return {"v": 1}

    @module(imports=[CacheModule.for_root()], controllers=[Cached])
    class CachedModule:
        pass

    client = TestClient(LaurenFactory.create(CachedModule, content=ContentConfig(codecs=[KvCodec()])))

    first_json = client.get("/cached/")
    first_kv = client.get("/cached/", headers={"accept": KV})
    again_json = client.get("/cached/", headers={"accept": "application/json, */*;q=0.1"})
    again_kv = client.get("/cached/", headers={"accept": f"{KV}, {JSON};q=0.5"})

    assert [r.headers["x-cache"] for r in (first_json, first_kv, again_json, again_kv)] == [
        "MISS",
        "MISS",
        "HIT",
        "HIT",
    ]
    assert again_kv.headers["content-type"] == KV and again_json.headers["content-type"] == JSON


# --- startup validation --------------------------------------------------------


def test_a_produces_entry_without_a_codec_fails_startup() -> None:
    with pytest.raises(StartupError) as excinfo:
        LaurenFactory.create(AppModule)

    detail = excinfo.value.detail
    assert detail["reason"] == "codec_not_registered"
    assert detail["media_type"] == KV
    assert detail["route"].startswith(("GET /users", "POST /users", "GET /kv"))


def test_a_consumes_entry_without_a_codec_fails_for_structured_bodies_only() -> None:
    @controller("/csv")
    class Csv:
        @post("/raw", consumes=["text/csv"])
        async def raw(self, body: Bytes) -> dict:
            return {}

    @controller("/csv-json")
    class CsvJson:
        @post("/", consumes=["text/csv"])
        async def parsed(self, body: Json[dict]) -> dict:
            return {}

    @module(controllers=[Csv])
    class RawModule:
        pass

    @module(controllers=[CsvJson])
    class ParsedModule:
        pass

    LaurenFactory.create(RawModule)
    with pytest.raises(StartupError) as excinfo:
        LaurenFactory.create(ParsedModule)

    assert excinfo.value.detail["reason"] == "codec_not_registered"
    assert excinfo.value.detail["media_type"] == "text/csv"
    assert excinfo.value.detail["route"] == "POST /csv-json"


def test_opaque_routes_may_document_any_produces() -> None:
    @controller("/report")
    class Report:
        @get("/", produces=["text/csv"])
        async def csv(self) -> Response:
            return Response.bytes(b"a,b", media_type="text/csv")

    @module(controllers=[Report])
    class ReportModule:
        pass

    response = TestClient(LaurenFactory.create(ReportModule)).get("/report/")

    assert response.headers["content-type"] == "text/csv"


def test_wildcard_consumes_does_not_parse_unregistered_media_as_json() -> None:
    @controller("/wild")
    class Wild:
        @post("/", consumes=["*/*"])
        async def ingest(self, body: Json[dict]) -> dict:
            return body

    @module(controllers=[Wild])
    class WildModule:
        pass

    client = TestClient(LaurenFactory.create(WildModule, content=ContentConfig(codecs=[KvCodec()])))

    unsupported = client.post("/wild/", content=b'{"x": 1}', headers={"content-type": "text/plain"})
    supported = client.post("/wild/", content=b"x=1", headers={"content-type": KV})

    assert unsupported.status_code == 415
    assert supported.json() == {"x": 1}


def test_registered_json_suffix_codec_decodes_inferred_json_body() -> None:
    class VendorCodec(KvCodec):
        media_types = ("application/vnd.kv+json",)

    @controller("/suffix")
    class Suffix:
        @post("/")
        async def ingest(self, body: Json[dict]) -> dict:
            return body

    @module(controllers=[Suffix])
    class SuffixModule:
        pass

    codec = VendorCodec()
    client = TestClient(
        LaurenFactory.create(SuffixModule, content=ContentConfig(codecs=[codec], strict_content_type=True))
    )

    response = client.post("/suffix/", content=b"x=1", headers={"content-type": codec.media_types[0]})

    assert response.json() == {"x": 1}


# --- typed fast paths -----------------------------------------------------------


def _fast_json_codec() -> JsonCodec:
    """A JSON codec whose fast path is on even when msgspec is not installed."""
    codec = JsonCodec()
    if not codec.fast:
        codec._loads = json.loads  # type: ignore[attr-defined]
    return codec


@pytest.mark.parametrize(
    "payload",
    [
        b'{"id": "x", "name": 3}',
        b'{"id": 1}',
        b"[1, 2]",
        b'{"id": 1, "name": "ok", "tags": "nope"}',
        b"{broken",
    ],
)
def test_pydantic_bodies_fail_identically_on_every_plan(payload: bytes) -> None:
    pydantic = pytest.importorskip("pydantic")

    class Model(pydantic.BaseModel):
        id: int
        name: str
        tags: list[str] = []

    @controller("/m")
    class Models:
        @post("/")
        async def create(self, body: Json[Model]) -> dict:
            return body.model_dump()

    @module(controllers=[Models])
    class ModelModule:
        pass

    def run(codec: JsonCodec) -> Any:
        client = TestClient(LaurenFactory.create(ModelModule, content=ContentConfig(codecs=[codec])))
        return client.post("/m/", content=payload, headers={"content-type": JSON})

    fast, generic = run(_fast_json_codec()), run(JsonCodec(backend="stdlib"))

    assert fast.status_code == generic.status_code in (400, 422)
    assert fast.content == generic.content


def test_pydantic_bodies_validate_in_one_pass_on_a_fast_codec() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Model(pydantic.BaseModel):
        id: int

    @controller("/one")
    class One:
        @post("/")
        async def create(self, body: Json[Model]) -> dict:
            return {"id": body.id}

    @module(controllers=[One])
    class OneModule:
        pass

    codec = _fast_json_codec()
    client = TestClient(LaurenFactory.create(OneModule, content=ContentConfig(codecs=[codec])))

    assert client.post("/one/", json={"id": 4}).json() == {"id": 4}


def test_msgpack_test_client_helpers_send_and_decode() -> None:
    pytest.importorskip("msgspec")
    from lauren import MsgpackCodec

    @controller("/mp")
    class Mp:
        @post("/", consumes=["application/msgpack"], produces=["application/msgpack"])
        async def echo(self, body: Json[dict]) -> dict:
            return body

    @module(controllers=[Mp])
    class MpModule:
        pass

    client = TestClient(LaurenFactory.create(MpModule, content=ContentConfig(codecs=[MsgpackCodec()])))

    response = client.post("/mp/", msgpack={"count": 3})

    assert response.status_code == 200
    assert response.request.headers["content-type"] == "application/msgpack"
    assert response.msgpack() == {"count": 3}


async def test_async_msgpack_test_client_helper() -> None:
    pytest.importorskip("msgspec")
    from lauren import MsgpackCodec
    from lauren.testing import AsyncTestClient

    @controller("/mp")
    class Mp:
        @post("/", consumes=["application/msgpack"], produces=["application/msgpack"])
        async def echo(self, body: Json[dict]) -> dict:
            return body

    @module(controllers=[Mp])
    class MpModule:
        pass

    app = LaurenFactory.create(MpModule, content=ContentConfig(codecs=[MsgpackCodec()]))
    async with AsyncTestClient(app) as client:
        response = await client.post("/mp/", msgpack={"count": 3})

    assert response.msgpack() == {"count": 3}


def test_msgpack_body_cannot_be_combined_with_other_body_sources() -> None:
    client = _client()

    with pytest.raises(TypeError, match="msgpack= cannot be combined"):
        client.post("/users/kv", msgpack={"id": 1}, json={"id": 1})


# --- docs and reflection ----------------------------------------------------------


def test_openapi_documents_consumes_and_produces() -> None:
    app = LaurenFactory.create(
        AppModule, content=ContentConfig(codecs=[KvCodec()]), openapi_url="/openapi.json"
    )
    spec = TestClient(app).get("/openapi.json").json()

    show = spec["paths"]["/users/{id}"]["get"]
    untyped = spec["paths"]["/users/untyped/{id}"]["get"]
    accept_kv = spec["paths"]["/users/kv"]["post"]
    plain = spec["paths"]["/users/plain"]["get"]
    assert list(show["responses"]["200"]["content"]) == [JSON, KV]
    assert list(untyped["responses"]["200"]["content"]) == [JSON, KV]
    assert list(accept_kv["requestBody"]["content"]) == [KV, JSON]
    assert list(plain["responses"]["200"]["content"]) == [JSON]


def test_reflection_reports_media_types() -> None:
    static = {r.path: (r.consumes, r.produces) for r in reflect_routes(Users)}
    client = _client()
    with client:
        effective = {(r.method, r.full_path): (r.consumes, r.produces) for r in get_all_routes(client.app)}

    assert static["/{id}"] == ((), (JSON, KV))
    assert static["/kv"] == ((KV, JSON), ())
    assert effective[("POST", "/users")] == (("application/json", "application/*+json"), (JSON, KV))
    assert effective[("GET", "/users/plain")] == ((), (JSON,))


# --- MessagePack (needs msgspec) -----------------------------------------------------


def test_msgpack_round_trips_requests_and_responses() -> None:
    pytest.importorskip("msgspec")
    from lauren import MsgpackCodec

    @controller("/mp")
    class Mp:
        @post("/", consumes=[JSON, "application/msgpack"], produces=[JSON, "application/msgpack"])
        async def echo(self, body: Json[User]) -> User:
            return body

    @module(controllers=[Mp])
    class MpModule:
        pass

    codec = MsgpackCodec()
    client = TestClient(LaurenFactory.create(MpModule, content=ContentConfig(codecs=[codec])))

    response = client.post(
        "/mp/",
        content=codec.encode({"id": 5, "name": "em"}),
        headers={"content-type": "application/msgpack", "accept": "application/msgpack"},
    )

    assert response.headers["content-type"] == "application/msgpack"
    assert codec.decode(response.content) == {"id": 5, "name": "em"}
    malformed = client.post("/mp/", content=b"\xc1", headers={"content-type": "application/msgpack"})
    assert malformed.status_code == 422
    assert malformed.json()["error"]["detail"]["issues"][0]["type"] == "body_invalid"


def test_negotiated_page_keeps_filtered_items_envelope_and_navigation() -> None:
    from lauren import OffsetPagination, Page

    @dataclass
    class InternalUser(User):
        password: str

    @controller("/negotiated-pages")
    class Pages:
        @get("/", produces=[JSON, KV])
        async def show(self, paging: OffsetPagination) -> Page[User]:
            return Page.of([InternalUser(1, "ada", "private")], paging, total=1)

    @module(controllers=[Pages])
    class AppModule:
        pass

    app = LaurenFactory.create(
        AppModule,
        response_validation="filter",
        content=ContentConfig(codecs=[KvCodec()]),
    )
    with TestClient(app) as client:
        response = client.get("/negotiated-pages?limit=1", headers={"accept": KV})

    assert response.status_code == 200
    assert response.headers.get("content-type") == KV
    assert KvCodec().decode(response.body)["items"] == [{"id": 1, "name": "ada"}]
    assert "private" not in response.text
    assert 'rel="first"' in response.headers.get("link", "")
    assert response.headers.get("vary") == "Accept"
