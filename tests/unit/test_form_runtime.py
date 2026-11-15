"""Form-cache failures, URL-encoded budgets and custom upload policies."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lauren import ContentConfig, MultipartConfig, UploadFile
from lauren._codecs._plan import resolve_content
from lauren._forms._config import _PartSpec, _UploadSpec, resolve_multipart
from lauren._forms._runtime import _UploadOwner, _check_sniff, read_form_data
from lauren._multipart import _Part
from lauren.exceptions import (
    BadRequestError,
    ExtractorError,
    RequestBodyTooLarge,
    StartupError,
    UnsupportedMediaTypeError,
)
from lauren.types import Headers, Request


def _request(body, *, media="application/x-www-form-urlencoded", config=None, status=422, content=True):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(method="POST", path="/", receive=receive, headers=Headers([("content-type", media)]))
    if content:
        request.__dict__["__lauren_content__"] = resolve_content(
            ContentConfig(multipart=config, malformed_body_status=status),
            sessions=False,
        )
    return request


@pytest.mark.parametrize(
    "config,body,reason",
    [
        (MultipartConfig(max_fields=1), b"a=1&b=2", "too_many_fields"),
        (MultipartConfig(max_field_size=2), b"a=abc", "field_too_large"),
    ],
)
async def test_urlencoded_extraction_enforces_text_budgets(config, body, reason) -> None:
    with pytest.raises(RequestBodyTooLarge) as caught:
        await read_form_data(_request(body, config=config))
    assert caught.value.detail["reason"] == reason


@pytest.mark.parametrize("status,error", [(422, ExtractorError), (400, BadRequestError)])
async def test_urlencoded_invalid_utf8_follows_malformed_body_policy(status, error) -> None:
    with pytest.raises(error) as caught:
        await read_form_data(_request(b"x=\xff", status=status))
    assert caught.value.detail["reason"] == "malformed_body"


@pytest.mark.parametrize("media,body", [("application/json", b"{}"), ("", b"x")])
async def test_unsupported_form_type_is_cached_as_the_same_failure(media, body) -> None:
    request = _request(body, media=media)
    with pytest.raises(UnsupportedMediaTypeError) as first:
        await read_form_data(request)
    with pytest.raises(UnsupportedMediaTypeError) as second:
        await read_form_data(request)
    assert first.value is second.value


async def test_missing_content_type_with_no_body_reaches_field_validation() -> None:
    assert not await read_form_data(_request(b"", media=""))


async def test_standalone_request_uses_default_form_config_and_shared_cache() -> None:
    request = _request(b"x=1&x=2", content=False)
    form = await read_form_data(request)
    assert await read_form_data(request) is form
    assert form.getall("x") == ["1", "2"]
    with pytest.raises(UnsupportedMediaTypeError):
        await request.multipart()


@pytest.mark.parametrize("directory", [5, b"not-a-text-path", "\x00"])
def test_temp_directory_type_and_invalid_paths_fail_at_startup(directory) -> None:
    with pytest.raises(StartupError):
        resolve_multipart(MultipartConfig(temp_dir=directory))


@pytest.mark.parametrize("result", ["application/custom", None, 5])
def test_custom_sniffer_has_an_explicit_return_contract(result) -> None:
    part = _Part("file", "x", "application/custom", b"", [])
    spec = _UploadSpec(content_types=("application/custom",), sniff=lambda prefix: result)
    if result is None:
        with pytest.raises(ExtractorError):
            _check_sniff(part, (spec,), b"custom")
    elif isinstance(result, str):
        _check_sniff(part, (spec,), b"custom")
    else:
        with pytest.raises(TypeError):
            _check_sniff(part, (spec,), b"custom")


async def test_cleanup_logs_a_close_failure_and_still_attempts_other_files(monkeypatch, caplog) -> None:
    owner = _UploadOwner()
    bad = UploadFile(data=b"", filename="bad", content_type="text/plain")
    good = UploadFile(data=b"", filename="good", content_type="text/plain")
    original = type(bad).close

    async def close(self):
        if self is bad:
            raise OSError("simulated close failure")
        await original(self)

    monkeypatch.setattr(type(bad), "close", close)
    owner.files = [bad, good]
    try:
        await owner.close()
        assert good.file.closed
        assert "Failed to close an uploaded file" in caplog.text
        assert repr(good) == "UploadFile(filename='good', content_type='text/plain', size=0)"
    finally:
        bad.file.close()


async def test_concurrent_multipart_calls_share_the_parse_and_its_resource_owner() -> None:
    started = asyncio.Event()
    proceed = asyncio.Event()
    calls = 0
    body = b'--B\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\nx\r\n--B--\r\n'

    async def receive():
        nonlocal calls
        calls += 1
        started.set()
        await proceed.wait()
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        method="POST",
        path="/",
        receive=receive,
        headers=Headers([("content-type", "multipart/form-data; boundary=B")]),
    )
    first = asyncio.create_task(request.multipart())
    await started.wait()
    second = asyncio.create_task(request.multipart())
    proceed.set()
    try:
        one, two = await asyncio.gather(first, second)
        assert one is two
        assert calls == 1
        assert request.__dict__["__lauren_upload_owner__"].files == [one["file"]]
    finally:
        await request.__dict__["__lauren_upload_owner__"].close()
    assert one["file"].file.closed


async def test_concurrent_failed_parses_share_the_cached_error() -> None:
    request = _request(b"not multipart", media="multipart/form-data; boundary=B")
    original = request._receive
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def receive():
        started.set()
        await proceed.wait()
        return await original()

    request._receive = receive
    first = asyncio.create_task(request.multipart())
    await started.wait()
    second = asyncio.create_task(request.multipart())
    proceed.set()
    failures = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(failures[0], ExtractorError)
    assert failures[0] is failures[1]


async def test_eof_delimiter_and_exact_sniff_prefix_do_not_lose_bytes() -> None:
    prefix = b"\x89PNG\r\n\x1a\n" + b"x" * 504
    body = (
        b'--B\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\n'
        b"Content-Type: image/png\r\n\r\n" + prefix + b"\r\n--B--"
    )
    request = _request(body, media="multipart/form-data; boundary=B")
    request.__dict__["__lauren_form_plan__"] = SimpleNamespace(
        rules={"file": _PartSpec("file", (_UploadSpec(sniff=True),))},
        takes_all=False,
        legacy_urlencoded=False,
    )
    try:
        form = await request.multipart()
        contents = await form["file"].read()
        assert contents == prefix
    finally:
        await request.__dict__["__lauren_upload_owner__"].close()


def test_merged_upload_rules_can_combine_sniffing_with_other_constraints() -> None:
    part = _Part("file", "x.png", "image/png", b"", [])
    _check_sniff(part, (_UploadSpec(max_size=100), _UploadSpec(sniff=True)), b"\x89PNG\r\n\x1a\n")


async def test_request_stream_cannot_be_started_again_after_consumption() -> None:
    request = _request(b"x")
    chunks = [chunk async for chunk in request.stream()]
    assert chunks == [b"x"]
    with pytest.raises(RuntimeError, match="already been consumed"):
        _ = [chunk async for chunk in request.stream()]
