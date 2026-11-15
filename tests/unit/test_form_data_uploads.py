"""Form values retain order, own bounded storage and expose real file operations."""

from __future__ import annotations

import tempfile

import pytest

from lauren.types import FormData, UploadFile


def test_form_data_is_a_first_value_multi_mapping() -> None:
    form = FormData([("a", "1"), ("b", "2"), ("a", "3")])
    values = form.getall("a")
    values.clear()
    assert dict(form) == {"a": "1", "b": "2"}
    assert list(form.multi_items()) == [("a", "1"), ("b", "2"), ("a", "3")]
    assert form.getall("a") == ["1", "3"]
    assert form.getall("missing") == []
    with pytest.raises(KeyError):
        form["missing"]
    with pytest.raises(TypeError):
        form["a"] = "changed"  # type: ignore[index]


def test_form_data_rejects_non_form_values() -> None:
    with pytest.raises(TypeError):
        FormData([("x", 1)])  # type: ignore[list-item]


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("../x.png", "x.png"),
        (r"C:\x\y.png", "y.png"),
        ("a\x00\u202eb.txt", "ab.txt"),
        ("cafe\u0301.txt", "café.txt"),
        (None, None),
        ("..", None),
        ("NUL.txt", "_NUL.txt"),
    ],
)
async def test_upload_safe_filename_is_only_a_normalized_safe_basename(filename, expected) -> None:
    upload = UploadFile(data=b"", filename=filename, content_type="text/plain")
    async with upload:
        assert upload.safe_filename == expected


async def test_upload_cursor_writes_and_legacy_snapshot() -> None:
    async with UploadFile(data=b"abc", filename="a", content_type="text/plain") as upload:
        assert await upload.read(1) == b"a"
        assert upload.read_sync() == b"abc"
        assert await upload.read() == b"bc"
        await upload.seek(1)
        assert await upload.write(b"XYZ") == 3
        assert upload.size == 4
        await upload.seek(0)
        assert b"".join([chunk async for chunk in upload.stream(2)]) == b"aXYZ"
    assert upload.file.closed
    await upload.close()
    with pytest.raises(ValueError):
        await upload.read()


async def test_upload_save_preserves_cursor_and_does_not_overwrite(tmp_path) -> None:
    async with UploadFile(data=b"abc", filename="a", content_type="text/plain") as upload:
        await upload.read(1)
        destination = tmp_path / "copy"
        assert await upload.save(destination) == 3
        assert destination.read_bytes() == b"abc"
        assert await upload.read() == b"bc"
        with pytest.raises(FileExistsError):
            await upload.save(destination)
        assert await upload.save(destination, overwrite=True) == 3


async def test_upload_rolls_over_and_keeps_its_size(tmp_path) -> None:
    spool = tempfile.SpooledTemporaryFile(max_size=3, mode="w+b", dir=tmp_path)
    upload = UploadFile(data=b"", filename="a", content_type="text/plain", _file=spool)
    async with upload:
        await upload.write(b"abc")
        assert not spool._rolled
        await upload.write(b"d")
        assert spool._rolled
        await upload.seek(0)
        assert await upload.read() == b"abcd"
        assert upload.size == 4
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("size", [0, -1, True])
async def test_upload_stream_requires_a_positive_chunk_size(size) -> None:
    async with UploadFile(data=b"abc", filename="a", content_type="text/plain") as upload:
        with pytest.raises(ValueError):
            _ = [part async for part in upload.stream(size)]
