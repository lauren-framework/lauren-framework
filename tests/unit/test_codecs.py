"""Body codecs: ``JsonCodec`` backends, ``MsgpackCodec``, the registry and media types (F03 §5.5)."""

from __future__ import annotations

import builtins
import json
from typing import Any

import pytest

from lauren import Codec, ContentConfig, JsonCodec, MsgpackCodec
from lauren._codecs._media import is_json_media, matches, normalize_media_range, split_media_type
from lauren._codecs._plan import CodecRegistry, resolve_content
from lauren.exceptions import StartupError
from lauren.serialization import StdlibJSONEncoder

#: Documents every backend must decode to the same value.
CORPUS: list[bytes] = [
    b'{"a": 1, "b": [true, false, null], "c": "x"}',
    b"[1, 2.5, -3e2, 0.1]",
    '{"name": "Zoë ☃", "emoji": "\\ud83d\\ude00"}'.encode(),
    b'"plain string"',
    b"12345678901234567890123456789",  # beyond 64 bits: stdlib decides
    b'{"n": NaN, "i": Infinity}',  # not RFC 8259: stdlib accepts it
    b'{"dup": 1, "dup": 2}',
    '{"utf16": true}'.encode("utf-16"),
]


def _installed(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


BACKENDS = ["auto", "stdlib"] + [b for b in ("orjson", "msgspec") if _installed(b)]
#: The documented orjson difference (it is never picked by ``"auto"``).
HUGE_INT = b"12345678901234567890123456789"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("document", CORPUS)
def test_json_backends_decode_like_the_stdlib(backend: str, document: bytes) -> None:
    if backend == "orjson" and document == HUGE_INT:
        pytest.skip("orjson decodes integers beyond 64 bits as floats (documented, opt-in)")
    expected = json.loads(document)

    decoded = JsonCodec(backend=backend).decode(document)

    assert repr(decoded) == repr(expected)


def test_auto_backend_never_picks_orjson() -> None:
    assert JsonCodec().backend in ("msgspec", "stdlib")


@pytest.mark.parametrize("backend", BACKENDS)
def test_json_backends_report_the_stdlib_error(backend: str) -> None:
    with pytest.raises(json.JSONDecodeError) as excinfo:
        JsonCodec(backend=backend).decode(b'{"a": ')

    assert (excinfo.value.lineno, excinfo.value.colno) == (1, 7)


def test_stdlib_backend_is_not_fast() -> None:
    codec = JsonCodec(backend="stdlib")

    assert (codec.backend, codec.fast) == ("stdlib", False)
    assert repr(codec) == "JsonCodec(backend='stdlib')"


def test_invalid_json_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="backend='simdjson'"):
        JsonCodec(backend="simdjson")


@pytest.mark.parametrize("backend", ["orjson", "msgspec"])
def test_a_missing_fast_backend_has_an_install_hint(backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == backend:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    with pytest.raises(RuntimeError, match=f"pip install {backend}"):
        JsonCodec(backend=backend)


def test_auto_backend_falls_back_to_the_stdlib(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in ("orjson", "msgspec"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    assert JsonCodec().backend == "stdlib"


def test_json_encode_uses_the_given_or_active_encoder() -> None:
    assert JsonCodec(encoder=StdlibJSONEncoder()).encode({"a": [1]}) == b'{"a":[1]}'
    assert json.loads(JsonCodec().encode({"a": 1})) == {"a": 1}


def test_builtin_codecs_satisfy_the_protocol() -> None:
    assert isinstance(JsonCodec(), Codec)


def test_msgpack_codec_round_trips() -> None:
    pytest.importorskip("msgspec")
    codec = MsgpackCodec()
    value = {"a": 1, "b": [1.5, "x", None, True], "c": {"nested": b"raw"}}

    assert codec.decode(codec.encode(value)) == value
    assert codec.media_types[0] == "application/msgpack"
    assert repr(codec) == "MsgpackCodec()"
    assert isinstance(codec, Codec)


def test_msgpack_codec_needs_msgspec(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "msgspec":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    with pytest.raises(RuntimeError, match="pip install msgspec"):
        MsgpackCodec()


# --- registry --------------------------------------------------------------


class CsvCodec:
    media_types = ("text/csv",)

    def decode(self, data: bytes) -> Any:
        return [row.split(",") for row in data.decode().splitlines()]

    def encode(self, value: Any) -> bytes:
        return "\n".join(",".join(map(str, row)) for row in value).encode()


def test_registry_always_has_json_and_maps_json_suffixes_to_it() -> None:
    registry = CodecRegistry([CsvCodec()])

    assert isinstance(registry.json, JsonCodec)
    assert registry.lookup("application/json") is registry.json
    assert registry.lookup("application/merge-patch+json") is registry.json
    assert isinstance(registry.lookup("text/csv"), CsvCodec)
    assert registry.lookup("text/xml") is None


def test_a_configured_json_codec_replaces_the_builtin_one() -> None:
    mine = JsonCodec(backend="stdlib")

    assert CodecRegistry([mine]).json is mine


@pytest.mark.parametrize(
    "codecs",
    [
        [CsvCodec(), CsvCodec()],
        [JsonCodec(), JsonCodec()],
        [type("FakeJson", (CsvCodec,), {"media_types": ("application/json",)})()],
    ],
)
def test_two_codecs_claiming_one_media_type_conflict(codecs: list[Any]) -> None:
    with pytest.raises(StartupError) as excinfo:
        CodecRegistry(codecs)

    assert excinfo.value.detail["reason"] == "codec_conflict"
    assert len(excinfo.value.detail["codecs"]) == 2


@pytest.mark.parametrize(
    "codec",
    [
        object(),
        type("NoTypes", (CsvCodec,), {"media_types": ()})(),
        type("StrTypes", (CsvCodec,), {"media_types": "text/csv"})(),
        type("Wild", (CsvCodec,), {"media_types": ("text/*",)})(),
    ],
)
def test_invalid_codecs_fail_startup(codec: Any) -> None:
    with pytest.raises(StartupError) as excinfo:
        CodecRegistry([codec])

    assert excinfo.value.detail["reason"] == "invalid_content_config"


# --- ContentConfig resolution -----------------------------------------------


def test_content_config_lists_become_tuples() -> None:
    config = ContentConfig(codecs=[CsvCodec()], default_produces=["application/json"])

    assert isinstance(config.codecs, tuple) and config.default_produces == ("application/json",)


@pytest.mark.parametrize(
    ("config", "key"),
    [
        (ContentConfig(malformed_body_status=500), "malformed_body_status"),
        (ContentConfig(malformed_body_status=True), "malformed_body_status"),
        (ContentConfig(strict_accept="yes"), "strict_accept"),  # type: ignore[arg-type]
        (ContentConfig(default_produces=()), "default_produces"),
        (ContentConfig(default_produces="application/json"), "default_produces"),  # type: ignore[arg-type]
        (ContentConfig(default_produces=("application/*",)), "default_produces"),
    ],
)
def test_invalid_content_config_fails_startup(config: ContentConfig, key: str) -> None:
    with pytest.raises(StartupError) as excinfo:
        resolve_content(config, sessions=False)

    assert excinfo.value.detail["reason"] == "invalid_content_config"
    assert key in excinfo.value.detail


@pytest.mark.parametrize("codecs", ["json", 3])
def test_codecs_must_be_a_list(codecs: Any) -> None:
    with pytest.raises(StartupError) as excinfo:
        resolve_content(ContentConfig(codecs=codecs), sessions=False)

    assert excinfo.value.detail["reason"] == "invalid_content_config"


def test_default_produces_needs_a_codec() -> None:
    with pytest.raises(StartupError) as excinfo:
        resolve_content(ContentConfig(default_produces=("text/csv",)), sessions=False)

    assert excinfo.value.detail == {"reason": "codec_not_registered", "media_type": "text/csv", "route": None}


def test_default_produces_is_normalised_and_deduplicated() -> None:
    runtime = resolve_content(
        ContentConfig(codecs=[CsvCodec()], default_produces=["TEXT/CSV", "text/csv", "application/json"]),
        sessions=False,
    )

    assert runtime.default_produces == ("text/csv", "application/json")


# --- media types -------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "wildcards", "expected"),
    [
        ("Application/JSON", False, "application/json"),
        (" text/csv ", False, "text/csv"),
        ("*/*", True, "*/*"),
        ("application/*", True, "application/*"),
        ("application/*+json", True, "application/*+json"),
    ],
)
def test_media_ranges_normalise(entry: str, wildcards: bool, expected: str) -> None:
    assert normalize_media_range(entry, wildcards=wildcards) == expected


@pytest.mark.parametrize(
    ("entry", "wildcards"),
    [
        (1, True),
        ("json", True),
        ("application/json; charset=utf-8", True),
        ("application/*", False),
        ("*/json", True),
        ("app lication/json", True),
        ("application/js on", True),
        ("application/*+", True),
        ("a/b/c", True),
    ],
)
def test_invalid_media_ranges_are_rejected(entry: Any, wildcards: bool) -> None:
    with pytest.raises(ValueError):
        normalize_media_range(entry, wildcards=wildcards)


@pytest.mark.parametrize(
    ("media_range", "media", "expected"),
    [
        ("*/*", "text/csv", True),
        ("text/*", "text/csv", True),
        ("text/*", "application/json", False),
        ("application/*+json", "application/merge-patch+json", True),
        ("application/*+json", "application/json", False),
        ("application/*+json", "application/+json", False),
        ("text/csv", "text/csv", True),
        ("text/csv", "text/plain", False),
    ],
)
def test_media_range_matching(media_range: str, media: str, expected: bool) -> None:
    assert matches(media_range, media) is expected


def test_split_media_type_and_json_detection() -> None:
    assert split_media_type('Application/JSON; Charset="UTF-8"; x') == (
        "application/json",
        {"charset": "UTF-8"},
    )
    assert is_json_media("application/problem+json") and not is_json_media("text/plain")
