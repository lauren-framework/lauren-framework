"""File-backed upload operations shared by the public value and marker."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unicodedata
from collections.abc import AsyncIterator, Callable
from functools import partial
from typing import Any, BinaryIO, TypeVar, cast

import anyio

_R = TypeVar("_R")


class _UploadStorage:
    """Own one seekable file; move potentially blocking operations to workers."""

    __slots__ = (
        "_file",
        "_size",
        "_disk",
        "_threshold",
        "_lock",
        "filename",
        "content_type",
        "headers",
        "name",
    )

    def __init__(
        self,
        *,
        data: bytes,
        filename: str | None,
        content_type: str,
        headers: list[tuple[str, str]] | None = None,
        name: str = "",
        _file: BinaryIO | None = None,
    ) -> None:
        if _file is None:
            # This compatibility constructor receives already-buffered bytes.
            # Do not introduce synchronous disk I/O while constructing it.
            spool = tempfile.SpooledTemporaryFile(max_size=max(1024 * 1024, len(data) + 1), mode="w+b")
            spool.write(data)
            spool.seek(0)
            _file = cast(BinaryIO, spool)
        self._file: BinaryIO = _file
        self._size = len(data)
        self._disk = bool(getattr(_file, "_rolled", True))
        self._threshold = getattr(_file, "_max_size", 0)
        self._lock = threading.Lock()
        self.filename = filename
        self.content_type = content_type
        self.headers = list(headers or ())
        self.name = name

    @property
    def file(self) -> BinaryIO:
        """The underlying seekable binary file; direct access is synchronous."""
        return self._file

    @property
    def size(self) -> int:
        """The file size in bytes, also available after cleanup."""
        return self._size

    @property
    def safe_filename(self) -> str | None:
        """Return an NFC basename without separators, controls or device names.

        Prefer an application-generated name for storage; this helper does not
        validate extensions, grant overwrite permission or authenticate a file.
        """
        if self.filename is None:
            return None
        name = unicodedata.normalize("NFC", self.filename.replace("\\", "/").rsplit("/", 1)[-1])
        name = "".join(c for c in name if not unicodedata.category(c).startswith("C") and c not in '<>:"|?*')
        name = name.strip().rstrip(". ")
        if not name or name in (".", ".."):
            return None
        stem = name.split(".", 1)[0].upper()
        if stem in ("CON", "PRN", "AUX", "NUL") or (
            len(stem) == 4 and stem[:3] in ("COM", "LPT") and stem[3] in "123456789"
        ):
            name = "_" + name
        return name

    def _operate(self, method: str, *args: Any) -> Any:
        with self._lock:
            result = getattr(self._file, method)(*args)
            if method == "write":
                self._size = max(self._size, self._file.tell())
            return result

    async def _run(self, operation: Callable[..., _R], *args: Any, disk: bool = False) -> _R:
        if disk:
            # Mark before the first await: concurrent operations must not take
            # a blocking lock on the event loop during rollover or a disk write.
            self._disk = True
        if self._disk or getattr(self._file, "_rolled", False):
            return await anyio.to_thread.run_sync(partial(operation, *args))
        return operation(*args)

    async def read(self, size: int = -1) -> bytes:
        """Read from the current position; use ``seek(0)`` to read again."""
        return await self._run(self._operate, "read", size)

    def read_sync(self) -> bytes:
        """Return all contents without changing the cursor (legacy helper).

        This can block for a disk-backed file. Prefer the asynchronous methods
        inside handlers; retain this helper for synchronous code and fixtures.
        """
        with self._lock:
            position = self._file.tell()
            try:
                self._file.seek(0)
                return self._file.read()
            finally:
                self._file.seek(position)

    async def write(self, data: bytes) -> int:
        """Write at the current position and update ``size``."""
        rolls = bool(self._threshold and self._size + len(data) > self._threshold)
        return await self._run(self._operate, "write", data, disk=rolls)

    async def seek(self, offset: int, whence: int = 0) -> int:
        """Move the cursor using the binary file's seek semantics."""
        return await self._run(self._operate, "seek", offset, whence)

    async def stream(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """Yield bounded chunks from the current position."""
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        while chunk := await self.read(chunk_size):
            yield chunk

    def _save(self, path: str | os.PathLike[str], overwrite: bool) -> int:
        with self._lock:
            position = self._file.tell()
            try:
                self._file.seek(0)
                with open(path, "wb" if overwrite else "xb") as target:
                    shutil.copyfileobj(self._file, target, length=64 * 1024)
                return self._size
            finally:
                self._file.seek(position)

    async def save(self, path: str | os.PathLike[str], *, overwrite: bool = False) -> int:
        """Copy the whole file without changing its cursor or creating directories.

        Create a new destination exclusively unless ``overwrite=True`` is
        explicit. Use a trusted destination, not a client-supplied pathname.
        """
        return await anyio.to_thread.run_sync(partial(self._save, path, overwrite))

    async def close(self) -> None:
        """Close the backing file and remove any temporary storage; idempotent."""
        await self._run(self._operate, "close")

    async def __aenter__(self) -> _UploadStorage:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        # Pydantic calls this hook only when installed. Core imports never
        # require the optional dependency.
        try:
            from pydantic_core import core_schema
        except ImportError as exc:  # pragma: no cover - called by pydantic
            raise RuntimeError("UploadFile's Pydantic integration requires pydantic.") from exc
        return core_schema.is_instance_schema(_UploadStorage)

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: Any, handler: Any) -> dict[str, str]:
        return {"type": "string", "format": "binary"}

    def __repr__(self) -> str:
        return f"UploadFile(filename={self.filename!r}, content_type={self.content_type!r}, size={self.size})"
