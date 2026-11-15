"""Multipart framing, budgets and resource ownership across arbitrary chunks."""

from __future__ import annotations

import asyncio
import tempfile
from types import SimpleNamespace

import pytest

from lauren._codecs._plan import resolve_content
from lauren._forms._config import MultipartConfig, _PartSpec, _UploadSpec
from lauren._multipart import _MultipartParser, iter_parts, parse_boundary
from lauren.exceptions import BadRequestError, ExtractorError, RequestBodyTooLarge
from lauren.types import ContentConfig, Headers, Request


def _body(payload=b"abc", *, name="file", filename="a.txt", content_type="text/plain"):
    disposition = f'Content-Disposition: form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    return (
        f"--B\r\n{disposition}\r\nContent-Type: {content_type}\r\n\r\n".encode() + payload + b"\r\n--B--\r\n"
    )


def _request(chunks, *, config=None, rules=None, takes_all=True, status=422):
    iterator = iter(chunks)
    reads = []

    # Mark the final frame without materializing a potentially large iterator.
    pending = next(iterator, None)

    async def lookahead():
        nonlocal pending
        reads.append(1)
        if pending is None:
            raise AssertionError("the body was read twice")
        chunk = pending
        pending = next(iterator, None)
        return {"type": "http.request", "body": chunk, "more_body": pending is not None}

    request = Request(
        method="POST",
        path="/",
        receive=lookahead,
        max_body_size=200 * 1024 * 1024,
        headers=Headers([("content-type", "multipart/form-data; boundary=B")]),
    )
    request.__dict__["__lauren_content__"] = resolve_content(
        ContentConfig(multipart=config, malformed_body_status=status),
        sessions=False,
    )
    if rules is not None:
        request.__dict__["__lauren_form_plan__"] = SimpleNamespace(
            rules=rules, takes_all=takes_all, legacy_urlencoded=False
        )
    return request, reads


async def _close(request):
    owner = request.__dict__.get("__lauren_upload_owner__")
    if owner is not None:
        await owner.close()


@pytest.mark.parametrize("cut", range(len(_body()) + 1))
async def test_multipart_survives_every_single_chunk_boundary(cut) -> None:
    body = _body()
    request, reads = _request([body[:cut], body[cut:]])
    try:
        form = await request.multipart()
        assert await form["file"].read() == b"abc"
        assert await request.multipart() is form
        assert len(reads) == 2
    finally:
        await _close(request)
    assert form["file"].file.closed


def test_boundary_lookalikes_are_not_delimiters() -> None:
    payload = b"a\r\n--BX\r\n--B--not-final\r\n--B-\x00z"
    assert list(iter_parts(_body(payload), "B"))[0].data == payload


def test_quoted_semicolons_and_extended_filename_precedence() -> None:
    body = (
        b'--B\r\nContent-Disposition: form-data; name="x;y"; filename="old;name.txt"; '
        b"filename*=UTF-8''caf%C3%A9.txt\r\n\r\nx\r\n--B--"
    )
    part = list(iter_parts(body, "B"))[0]
    assert (part.name, part.filename) == ("x;y", "café.txt")
    assert parse_boundary('multipart/form-data; ignored="a;b"; boundary="B"') == "B"


def test_rfc2231_filename_continuations_can_split_a_utf8_character() -> None:
    body = (
        b'--B\r\nContent-Disposition: form-data; name="x"; '
        b"filename*0*=UTF-8''%E2; filename*1*=%82%AC.txt\r\n\r\nx\r\n--B--"
    )
    assert list(iter_parts(body, "B"))[0].filename == "€.txt"


@pytest.mark.parametrize(
    "payload,reason,config",
    [
        (_body(b"12345", filename=None), "field_too_large", MultipartConfig(max_field_size=4)),
        (_body(b"x") + b"", "part_headers_too_large", MultipartConfig(max_part_header_bytes=5)),
    ],
)
async def test_multipart_limits_are_413(payload, reason, config) -> None:
    request, _ = _request([payload], config=config)
    with pytest.raises(RequestBodyTooLarge) as caught:
        await request.multipart()
    assert caught.value.detail["reason"] == reason
    await _close(request)


async def test_file_limit_stops_receiving_and_closes_partial_upload() -> None:
    first = _body(b"x" * 100)[:-9]
    request, reads = _request(
        [first, b"x" * 100, b"\r\n--B--\r\n"], rules={"file": _PartSpec("file", (_UploadSpec(max_size=10),))}
    )
    with pytest.raises(RequestBodyTooLarge) as caught:
        await request.multipart()
    assert caught.value.detail["reason"] == "file_too_large"
    assert len(reads) == 1
    assert request.__dict__["__lauren_upload_owner__"].files == []


async def test_undeclared_files_are_drained_without_creating_a_spool(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("an undeclared file must not be spooled")

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", forbidden)
    request, _ = _request([_body(b"x" * 200_000)], rules={}, takes_all=False)
    assert not await request.multipart()
    await _close(request)


@pytest.mark.parametrize("status,error", [(422, ExtractorError), (400, BadRequestError)])
async def test_malformed_stream_closes_files_and_caches_failure(status, error) -> None:
    request, reads = _request(
        [_body(b"x" * 100)[:-9]], status=status, config=MultipartConfig(spool_threshold=4)
    )
    with pytest.raises(error) as first:
        await request.multipart()
    with pytest.raises(error) as second:
        await request.multipart()
    assert first.value is second.value
    assert len(reads) == 1
    assert request.__dict__["__lauren_upload_owner__"].files == []


async def test_cancellation_closes_a_disk_backed_partial_upload(monkeypatch) -> None:
    spools = []
    original = tempfile.SpooledTemporaryFile

    def tracked(*args, **kwargs):
        spool = original(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", tracked)
    waiting = asyncio.Event()
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": _body(b"x" * 100)[:-9], "more_body": True}
        waiting.set()
        await asyncio.Event().wait()
        raise AssertionError("the blocked receive unexpectedly resumed")

    request, _ = _request([], config=MultipartConfig(spool_threshold=4))
    request._receive = receive
    task = asyncio.create_task(request.multipart())
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(spools) == 1 and spools[0]._rolled and spools[0].closed


async def test_a_lower_route_limit_checks_previously_streamed_multipart_bytes() -> None:
    request, _ = _request([_body()])
    try:
        await request.multipart()
        with pytest.raises(RequestBodyTooLarge):
            request._set_body_limit(5)
        with pytest.raises(RuntimeError, match="already been consumed"):
            _ = await request.body()
    finally:
        await _close(request)


def test_parser_never_buffers_an_entire_large_asgi_frame() -> None:
    parser = _MultipartParser("B")
    maximum = 0
    for _ in parser.feed(_body(b"x" * 2_000_000)):
        maximum = max(maximum, len(parser._buffer))
    list(parser.finish())
    assert maximum <= 64 * 1024


@pytest.mark.parametrize("suffix", [b"\r\n", b" \t\r\n", b"--", b"-- \t\r\n"])
def test_delimiter_padding_is_safe_even_in_one_byte_chunks(suffix) -> None:
    body = _body(b"abc", filename=None)
    if suffix.startswith(b"--"):
        body = body[:-4] + suffix
    else:
        body = body.replace(b"--B\r\n", b"--B" + suffix, 1)
    parser = _MultipartParser("B")
    events = []
    for byte in body:
        events.extend(parser.feed(bytes([byte])))
    events.extend(parser.finish())
    assert b"".join(value for kind, value in events if kind == "data") == b"abc"


@pytest.mark.parametrize(
    "header",
    [
        'form-data; name="x',
        'form-data; name="x"junk',
        "form-data; name=x; filename*=UTF-8''%ZZ",
        "form-data; name=x; filename*=unknown''x",
        "form-data; name=x; filename*=UTF-8''%FF",
        "form-data; name=x; filename*=x",
        "form-data; name=x; filename*1=x",
        "form-data; name=x; filename*999999=x",
        "form-data; name=x; filename*0=x; filename*1*=y",
    ],
)
def test_invalid_quoted_or_extended_parameters_are_malformed(header) -> None:
    from lauren._multipart import _parse_header_parameters

    with pytest.raises(ExtractorError):
        _parse_header_parameters(header)


@pytest.mark.parametrize("boundary", ["x" * 71, "résumé", "bad\r\nvalue", "bad "])
def test_boundary_rejects_oversize_non_ascii_or_unsafe_values(boundary) -> None:
    with pytest.raises(ExtractorError):
        parse_boundary(f'multipart/form-data; boundary="{boundary}"')


@pytest.mark.parametrize(
    "header",
    [
        "Content-Disposition: attachment; name=x",
        "Content-Disposition: form-data; name=x\r\nContent-Disposition: form-data; name=x",
        "Bad Header: value\r\nContent-Disposition: form-data; name=x",
        "Content-Disposition: form-data; name=x\x00",
    ],
)
def test_invalid_part_headers_are_malformed(header) -> None:
    with pytest.raises(ExtractorError):
        list(iter_parts(b"--B\r\n" + header.encode() + b"\r\n\r\nx\r\n--B--", "B"))


def _multiple(*parts):
    return b"\r\n".join(part[:-9] for part in parts) + b"\r\n--B--\r\n"


@pytest.mark.parametrize(
    "reason,config,filename",
    [
        ("too_many_parts", MultipartConfig(max_parts=1), "x"),
        ("too_many_files", MultipartConfig(max_files=1), "x"),
        ("too_many_fields", MultipartConfig(max_fields=1), None),
    ],
)
async def test_part_and_kind_counts_include_every_part(reason, config, filename) -> None:
    request, _ = _request([_multiple(_body(filename=filename), _body(filename=filename))], config=config)
    with pytest.raises(RequestBodyTooLarge) as caught:
        await request.multipart()
    assert caught.value.detail["reason"] == reason
    assert request.__dict__["__lauren_upload_owner__"].files == []


@pytest.mark.parametrize(
    "kind,filename,reason", [("text", "x", "expected_text"), ("file", None, "expected_file")]
)
async def test_part_kind_is_checked_before_storage(kind, filename, reason) -> None:
    request, _ = _request([_body(filename=filename)], rules={"file": _PartSpec(kind)}, takes_all=False)
    with pytest.raises(ExtractorError) as caught:
        await request.multipart()
    assert caught.value.detail["reason"] == reason
    assert caught.value.detail["issues"][0]["loc"] == ["body", "file"]


async def test_per_field_file_count_is_a_422_not_a_global_budget_413() -> None:
    request, _ = _request(
        [_multiple(_body(), _body())], rules={"file": _PartSpec("file", (_UploadSpec(max_files=1),))}
    )
    with pytest.raises(ExtractorError) as caught:
        await request.multipart()
    assert caught.value.detail["reason"] == "too_many_files"


async def test_unknown_parts_can_be_rejected_before_storage() -> None:
    request, _ = _request(
        [_body()], config=MultipartConfig(undeclared_parts="reject"), rules={}, takes_all=False
    )
    with pytest.raises(ExtractorError) as caught:
        await request.multipart()
    assert caught.value.detail["reason"] == "undeclared_part"


@pytest.mark.parametrize(
    "media,payload",
    [
        ("image/png", b"\x89PNG\r\n\x1a\n"),
        ("image/jpeg", b"\xff\xd8\xff"),
        ("image/gif", b"GIF89a"),
        ("image/webp", b"RIFFxxxxWEBP"),
        ("application/pdf", b"%PDF-1.7"),
        ("application/zip", b"PK\x03\x04"),
        ("application/gzip", b"\x1f\x8b"),
    ],
)
@pytest.mark.parametrize("padding", [b"", b"x" * 1024])
async def test_builtin_sniffers_accept_matching_file_prefixes(media, payload, padding) -> None:
    payload += padding
    request, _ = _request(
        [_body(payload, content_type=media)],
        rules={"file": _PartSpec("file", (_UploadSpec(content_types=(media,), sniff=True),))},
    )
    try:
        form = await request.multipart()
        assert await form["file"].read() == payload
    finally:
        await _close(request)


@pytest.mark.parametrize(
    "spec,media,payload,reason",
    [
        (_UploadSpec(content_types=("image/png",)), "text/plain", b"x", "content_type_not_allowed"),
        (_UploadSpec(content_types=("image/png",), sniff=True), "image/png", b"x", "content_mismatch"),
        (_UploadSpec(content_types=("image/png",), sniff=True), "image/png", b"x" * 1024, "content_mismatch"),
    ],
)
async def test_declared_and_detected_types_are_independent_checks(spec, media, payload, reason) -> None:
    request, _ = _request([_body(payload, content_type=media)], rules={"file": _PartSpec("file", (spec,))})
    with pytest.raises(ExtractorError) as caught:
        await request.multipart()
    assert caught.value.detail["reason"] == reason
    assert request.__dict__["__lauren_upload_owner__"].files == []


async def test_invalid_utf8_text_is_malformed_not_an_internal_error() -> None:
    request, _ = _request([_body(b"\xff", filename=None)])
    with pytest.raises(ExtractorError) as caught:
        await request.multipart()
    assert caught.value.detail["reason"] == "malformed_body"
