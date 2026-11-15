"""One bounded form parse and deterministic request-owned upload cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import Mapping
from typing import Any, BinaryIO, cast
from urllib.parse import parse_qsl

import anyio

from .._codecs._media import matches, media_type_of
from .._multipart import _MultipartParser, _Part, _malformed, parse_boundary
from ..exceptions import (
    BadRequestError,
    ExtractorError,
    ExtractorFieldError,
    RequestBodyTooLarge,
    UnsupportedMediaTypeError,
)
from ..types import FormData, Request, UploadFile
from ._config import MultipartConfig, _PartSpec, _UploadSpec, resolve_multipart

_FORM_CACHE = "__lauren_form_data__"
_FORM_ERROR = "__lauren_form_error__"
_UPLOAD_OWNER = "__lauren_upload_owner__"
_FORM_PLAN = "__lauren_form_plan__"
_CONTENT = "__lauren_content__"
_SNIFF_BYTES = 512


def _field_error(name: str, reason: str, message: str, **ctx: Any) -> ExtractorFieldError:
    return ExtractorFieldError.from_issues(
        [{"loc": ["body", name], "type": reason, "msg": message, **({"ctx": ctx} if ctx else {})}],
        field=name,
        message=message,
        detail={"reason": reason, "field": name, **ctx},
    )


def _limit(reason: str, **ctx: Any) -> RequestBodyTooLarge:
    return RequestBodyTooLarge(
        "multipart body exceeds its configured resource limit", detail={"reason": reason, **ctx}
    )


def _builtin_sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip"
    if data.startswith(b"\x1f\x8b"):
        return "application/gzip"
    return None


def _check_type(part: _Part, specs: tuple[_UploadSpec, ...]) -> None:
    media = media_type_of(part.content_type)
    for spec in specs:
        if spec.content_types and not any(matches(allowed, media) for allowed in spec.content_types):
            raise _field_error(
                part.name, "content_type_not_allowed", "file content type is not allowed", content_type=media
            )


def _check_sniff(part: _Part, specs: tuple[_UploadSpec, ...], prefix: bytes) -> None:
    for spec in specs:
        if not spec.sniff:
            continue
        detected = _builtin_sniff(prefix) if spec.sniff is True else spec.sniff(prefix)
        if detected is not None and not isinstance(detected, str):
            raise TypeError("UploadField.sniff must return a media type string or None")
        detected = media_type_of(detected) if detected else None
        if (
            detected is None
            or detected != media_type_of(part.content_type)
            or (spec.content_types and not any(matches(allowed, detected) for allowed in spec.content_types))
        ):
            raise _field_error(
                part.name, "content_mismatch", "file contents do not match the declared content type"
            )


class _UploadOwner:
    def __init__(self) -> None:
        self.files: list[UploadFile] = []

    async def close(self) -> None:
        # AnyIO cancellation scopes must not interrupt cleanup. A cancelled
        # disk write and this close also synchronize on the upload's I/O lock.
        with anyio.CancelScope(shield=True):
            files, self.files = self.files, []
            for upload in files:
                try:
                    await upload.close()
                except Exception:
                    logging.getLogger("lauren.multipart").exception("Failed to close an uploaded file")


class _PartSink:
    def __init__(
        self,
        part: _Part,
        rule: _PartSpec | None,
        *,
        capture: bool,
        config: MultipartConfig,
        owner: _UploadOwner,
    ) -> None:
        self.part = part
        self.specs = rule.uploads if rule else ()
        self.config = config
        self.size = 0
        self.text = bytearray()
        self.prefix = bytearray()
        self.sniffed = not any(spec.sniff for spec in self.specs)
        self.upload: UploadFile | None = None
        self.discard = part.filename is not None and not capture
        if part.filename is not None and capture:
            _check_type(part, self.specs)
            # The public marker is also the value type, including for optional
            # backend instance validation. Import after extractors has loaded.
            from ..extractors import UploadFile as PublicUploadFile

            directory = os.fspath(config.temp_dir) if config.temp_dir is not None else None
            spool = tempfile.SpooledTemporaryFile(max_size=config.spool_threshold, mode="w+b", dir=directory)
            self.upload = PublicUploadFile(
                data=b"",
                filename=part.filename,
                content_type=part.content_type,
                headers=part.headers,
                name=part.name,
                _file=cast(BinaryIO, spool),
            )
            owner.files.append(self.upload)

    async def feed(self, data: bytes) -> None:
        self.size += len(data)
        for spec in self.specs:
            if spec.max_size is not None and self.size > spec.max_size:
                raise _limit("file_too_large", field=self.part.name, max_size=spec.max_size)
        if self.discard:
            return
        if self.upload is None:
            if self.size > self.config.max_field_size:
                raise _limit(
                    "field_too_large", field=self.part.name, max_field_size=self.config.max_field_size
                )
            self.text.extend(data)
            return
        if not self.sniffed:
            count = min(_SNIFF_BYTES - len(self.prefix), len(data))
            self.prefix.extend(data[:count])
            data = data[count:]
            if len(self.prefix) < _SNIFF_BYTES:
                return
            _check_sniff(self.part, self.specs, bytes(self.prefix))
            self.sniffed = True
            await self.upload.write(bytes(self.prefix))
            self.prefix.clear()
        if data:
            await self.upload.write(data)

    async def finish(self) -> str | UploadFile | None:
        if self.discard:
            return None
        if self.upload is None:
            try:
                return self.text.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _malformed("multipart text field is not UTF-8", field=self.part.name) from exc
        if not self.sniffed:
            _check_sniff(self.part, self.specs, bytes(self.prefix))
            await self.upload.write(bytes(self.prefix))
            self.prefix.clear()
            self.sniffed = True
        await self.upload.seek(0)
        return self.upload


async def _parse_multipart(request: Request, config: MultipartConfig) -> FormData:
    boundary = parse_boundary(request.headers.get("content-type", ""))
    if request.__dict__.get("__lauren_form_buffer_body__"):
        # Another declared body extractor needs replay. Buffering is explicit
        # in the compiled route; ordinary form/file-only routes stay streamed.
        await request.body()
    parser = _MultipartParser(boundary, max_header_bytes=config.max_part_header_bytes)
    plan = request.__dict__.get(_FORM_PLAN)
    rules: Mapping[str, _PartSpec] = plan.rules if plan else {}
    takes_all = plan is None or plan.takes_all
    owner = _UploadOwner()
    request.__dict__[_UPLOAD_OWNER] = owner
    pairs: list[tuple[str, str | UploadFile]] = []
    parts = fields = files = 0
    counts: dict[str, int] = {}
    sink: _PartSink | None = None
    complete = False

    async def event(kind: str, value: _Part | bytes | None) -> None:
        nonlocal parts, fields, files, sink
        if kind == "part":
            assert isinstance(value, _Part)
            parts += 1
            if parts > config.max_parts:
                raise _limit("too_many_parts", max_parts=config.max_parts)
            is_file = value.filename is not None
            if is_file:
                files += 1
                if files > config.max_files:
                    raise _limit("too_many_files", max_files=config.max_files)
            else:
                fields += 1
                if fields > config.max_fields:
                    raise _limit("too_many_fields", max_fields=config.max_fields)
            rule = rules.get(value.name)
            if rule is None and not takes_all and config.undeclared_parts == "reject":
                raise _field_error(value.name, "undeclared_part", "multipart part is not declared")
            if rule is not None and is_file != (rule.kind == "file"):
                reason = "expected_file" if rule.kind == "file" else "expected_text"
                raise _field_error(value.name, reason, "multipart part has the wrong kind")
            if is_file:
                counts[value.name] = counts.get(value.name, 0) + 1
                for spec in rule.uploads if rule else ():
                    if spec.max_files is not None and counts[value.name] > spec.max_files:
                        raise _field_error(
                            value.name,
                            "too_many_files",
                            "too many files for this field",
                            max_files=spec.max_files,
                        )
            sink = _PartSink(value, rule, capture=takes_all or rule is not None, config=config, owner=owner)
        elif kind == "data":
            assert sink is not None and isinstance(value, bytes)
            await sink.feed(value)
        else:
            assert sink is not None
            result = await sink.finish()
            if result is not None:
                pairs.append((sink.part.name, result))
            sink = None

    try:
        async for chunk in request.stream():
            for kind, value in parser.feed(chunk):
                await event(kind, value)
        for kind, value in parser.finish():
            await event(kind, value)
        result = FormData(pairs)
        complete = True
        return result
    finally:
        if not complete:
            await owner.close()


async def _read_form_data_locked(request: Request) -> FormData:
    """Read and cache a form while holding its request-owned parse lock."""
    cached = request.__dict__.get(_FORM_CACHE)
    if cached is not None:
        return cached
    error = request.__dict__.get(_FORM_ERROR)
    if error is not None:
        raise error
    content = request.__dict__.get(_CONTENT)
    config = content.multipart if content is not None else resolve_multipart(None)
    try:
        media = media_type_of(request.headers.get("content-type", ""))
        plan = request.__dict__.get(_FORM_PLAN)
        legacy = (
            plan is not None and plan.legacy_urlencoded and content is not None and content.strict is not True
        )
        if media == "multipart/form-data":
            result = await _parse_multipart(request, config)
        elif not media and not legacy:
            if await request.body():
                raise UnsupportedMediaTypeError("a nonempty form body requires Content-Type")
            result = FormData()
        elif media == "application/x-www-form-urlencoded" or legacy:
            body = await request.body()
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _malformed("form body is not UTF-8") from exc
            try:
                pairs = parse_qsl(
                    text, keep_blank_values=True, max_num_fields=min(config.max_fields, config.max_parts)
                )
            except ValueError as exc:
                raise _limit("too_many_fields", max_fields=min(config.max_fields, config.max_parts)) from exc
            for name, value in pairs:
                if len(value.encode("utf-8")) > config.max_field_size:
                    raise _limit("field_too_large", field=name, max_field_size=config.max_field_size)
            result = FormData(pairs)
        else:
            raise UnsupportedMediaTypeError("expected a URL-encoded or multipart form body")
    except ExtractorError as exc:
        if (
            content is not None
            and content.malformed_status == 400
            and exc.detail.get("reason") == "malformed_body"
        ):
            error = BadRequestError(str(exc), detail=exc.detail)
            request.__dict__[_FORM_ERROR] = error
            raise error from exc
        request.__dict__[_FORM_ERROR] = exc
        raise
    except (Exception, asyncio.CancelledError) as exc:
        request.__dict__[_FORM_ERROR] = exc
        raise
    request.__dict__[_FORM_CACHE] = result
    request.__dict__["__lauren_form_validated_plan__"] = request.__dict__.get(_FORM_PLAN)
    return result


async def read_form_data(request: Request) -> FormData:
    """Share one parse, including concurrent callers and failed parses."""
    cached = request.__dict__.get(_FORM_CACHE)
    if cached is not None:
        return cached
    error = request.__dict__.get(_FORM_ERROR)
    if error is not None:
        raise error
    lock = request.__dict__.get("__lauren_form_parse_lock__")
    if lock is None:
        lock = asyncio.Lock()
        request.__dict__["__lauren_form_parse_lock__"] = lock
    async with lock:
        # Recheck after a peer completed its parse while this caller waited.
        return await _read_form_data_locked(request)


async def read_multipart(request: Request) -> FormData:
    if media_type_of(request.headers.get("content-type", "")) != "multipart/form-data":
        raise UnsupportedMediaTypeError("Request.multipart() requires multipart/form-data")
    return await read_form_data(request)
