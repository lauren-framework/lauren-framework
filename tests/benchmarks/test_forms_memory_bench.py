"""Streaming a large upload does not allocate its complete payload."""

from __future__ import annotations

import itertools
import tracemalloc

from lauren import ContentConfig, MultipartConfig
from lauren._codecs._plan import resolve_content
from lauren.types import Headers, Request


async def test_100_mib_multipart_upload_has_a_bounded_python_memory_peak(tmp_path) -> None:
    chunk = b"x" * (64 * 1024)
    frames = iter(
        itertools.chain(
            [b'--B\r\nContent-Disposition: form-data; name="file"; filename="large.bin"\r\n\r\n'],
            itertools.repeat(chunk, 1600),
            [b"\r\n--B--\r\n"],
        )
    )
    pending = next(frames)

    async def receive():
        nonlocal pending
        body, pending = pending, next(frames, None)
        return {"type": "http.request", "body": body, "more_body": pending is not None}

    request = Request(
        method="POST",
        path="/",
        receive=receive,
        max_body_size=128 * 1024 * 1024,
        headers=Headers([("content-type", "multipart/form-data; boundary=B")]),
    )
    request.__dict__["__lauren_content__"] = resolve_content(
        ContentConfig(multipart=MultipartConfig(temp_dir=tmp_path)),
        sessions=False,
    )
    tracemalloc.start()
    try:
        form = await request.multipart()
        _, peak = tracemalloc.get_traced_memory()
        assert form["file"].size == 100 * 1024 * 1024
        assert form["file"].file._rolled
        assert request._body is None
        # Include framing buffers, rollover and worker setup, not just the
        # spool's nominal threshold. A buffered parser allocates ~100 MiB.
        assert peak < 5 * 1024 * 1024, peak
        print(f"100 MiB upload, 1 MiB spool threshold: Python allocation peak {peak} bytes")
    finally:
        tracemalloc.stop()
        await request.__dict__["__lauren_upload_owner__"].close()
    assert form["file"].file.closed
