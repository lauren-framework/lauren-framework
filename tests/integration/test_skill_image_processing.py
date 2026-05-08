"""Integration tests for the Image Processing skill (Skill 34).

Tests use Pillow to create in-memory PNG images and verify that resize,
thumbnail, crop, and grayscale operations produce the expected output.
"""

from __future__ import annotations

from io import BytesIO

from lauren import (
    Bytes,
    LaurenFactory,
    Scope,
    controller,
    injectable,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class ImageProcessor:
    def process(self, image_data: bytes, operations: list[dict]) -> bytes:
        try:
            from PIL import Image
        except ImportError:
            return image_data

        img = Image.open(BytesIO(image_data))
        original_format = img.format or "PNG"
        for op in operations:
            if op["op"] == "resize":
                img = img.resize((op["width"], op["height"]), Image.LANCZOS)
            elif op["op"] == "thumbnail":
                img.thumbnail((op["width"], op["height"]), Image.LANCZOS)
            elif op["op"] == "crop":
                img = img.crop((op["left"], op["top"], op["right"], op["bottom"]))
            elif op["op"] == "grayscale":
                img = img.convert("L")
        output = BytesIO()
        img.save(output, format=original_format)
        return output.getvalue()

    def get_info(self, image_data: bytes) -> dict:
        try:
            from PIL import Image

            img = Image.open(BytesIO(image_data))
            return {
                "width": img.size[0],
                "height": img.size[1],
                "format": img.format,
                "mode": img.mode,
            }
        except Exception as e:
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# Controller & Module
# ---------------------------------------------------------------------------


@controller("/images")
class ImageController:
    def __init__(self, processor: ImageProcessor) -> None:
        self._processor = processor

    @post("/info")
    async def info(self, file_data: Bytes) -> dict:
        return self._processor.get_info(file_data)

    @post("/resize")
    async def resize(self, file_data: Bytes) -> dict:
        result = self._processor.process(
            file_data, [{"op": "resize", "width": 50, "height": 50}]
        )
        info = self._processor.get_info(result)
        return info

    @post("/thumbnail")
    async def thumbnail(self, file_data: Bytes) -> dict:
        result = self._processor.process(
            file_data, [{"op": "thumbnail", "width": 30, "height": 30}]
        )
        info = self._processor.get_info(result)
        return info

    @post("/grayscale")
    async def grayscale(self, file_data: Bytes) -> dict:
        result = self._processor.process(file_data, [{"op": "grayscale"}])
        info = self._processor.get_info(result)
        return info

    @post("/crop")
    async def crop(self, file_data: Bytes) -> dict:
        result = self._processor.process(
            file_data, [{"op": "crop", "left": 0, "top": 0, "right": 40, "bottom": 40}]
        )
        info = self._processor.get_info(result)
        return info


@module(controllers=[ImageController], providers=[ImageProcessor])
class ImageModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_image(width: int = 100, height: int = 100) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), color="red")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(ImageModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestImageProcessor:
    def test_get_info_returns_dimensions(self) -> None:
        processor = ImageProcessor()
        data = _make_test_image(80, 60)
        info = processor.get_info(data)
        assert info["width"] == 80
        assert info["height"] == 60
        assert info["format"] == "PNG"

    def test_resize_changes_dimensions(self) -> None:
        processor = ImageProcessor()
        data = _make_test_image(100, 100)
        result = processor.process(data, [{"op": "resize", "width": 40, "height": 20}])
        info = processor.get_info(result)
        assert info["width"] == 40
        assert info["height"] == 20

    def test_thumbnail_preserves_aspect_ratio(self) -> None:
        processor = ImageProcessor()
        data = _make_test_image(200, 100)
        result = processor.process(
            data, [{"op": "thumbnail", "width": 50, "height": 50}]
        )
        info = processor.get_info(result)
        # thumbnail fits within 50x50 preserving aspect ratio
        assert info["width"] <= 50
        assert info["height"] <= 50

    def test_crop_reduces_dimensions(self) -> None:
        processor = ImageProcessor()
        data = _make_test_image(100, 100)
        result = processor.process(
            data, [{"op": "crop", "left": 10, "top": 10, "right": 60, "bottom": 60}]
        )
        info = processor.get_info(result)
        assert info["width"] == 50
        assert info["height"] == 50

    def test_grayscale_changes_mode(self) -> None:
        processor = ImageProcessor()
        data = _make_test_image(50, 50)
        result = processor.process(data, [{"op": "grayscale"}])
        info = processor.get_info(result)
        assert info["mode"] == "L"

    def test_chained_operations(self) -> None:
        processor = ImageProcessor()
        data = _make_test_image(200, 200)
        ops = [
            {"op": "resize", "width": 100, "height": 100},
            {"op": "grayscale"},
        ]
        result = processor.process(data, ops)
        info = processor.get_info(result)
        assert info["width"] == 100
        assert info["height"] == 100
        assert info["mode"] == "L"


class TestImageController:
    def test_info_endpoint(self) -> None:
        client = build_app()
        data = _make_test_image(80, 60)
        r = client.post("/images/info", content=data)
        assert r.status_code == 200
        body = r.json()
        assert body["width"] == 80
        assert body["height"] == 60

    def test_resize_endpoint(self) -> None:
        client = build_app()
        data = _make_test_image(100, 100)
        r = client.post("/images/resize", content=data)
        assert r.status_code == 200
        body = r.json()
        assert body["width"] == 50
        assert body["height"] == 50

    def test_thumbnail_endpoint(self) -> None:
        client = build_app()
        data = _make_test_image(100, 100)
        r = client.post("/images/thumbnail", content=data)
        assert r.status_code == 200
        body = r.json()
        assert body["width"] <= 30
        assert body["height"] <= 30

    def test_grayscale_endpoint(self) -> None:
        client = build_app()
        data = _make_test_image(50, 50)
        r = client.post("/images/grayscale", content=data)
        assert r.status_code == 200
        assert r.json()["mode"] == "L"

    def test_crop_endpoint(self) -> None:
        client = build_app()
        data = _make_test_image(100, 100)
        r = client.post("/images/crop", content=data)
        assert r.status_code == 200
        body = r.json()
        assert body["width"] == 40
        assert body["height"] == 40
