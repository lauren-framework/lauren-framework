---
name: image-processing
description: Processes images (resize, thumbnail, crop, grayscale) via a Pillow-backed pipeline. Use when you need server-side image transformations before storing or serving user-uploaded images.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Image Processing Pipeline (Resize / Crop / Thumbnail)

## Overview

`ImageProcessor` accepts raw image bytes and an ordered list of operation
descriptors. Each descriptor is a dict with an `op` key. The service opens the
image with Pillow, applies operations in sequence, and returns the processed
bytes. If Pillow is not installed it returns the original bytes unchanged.

## ImageProcessor

```python
from io import BytesIO
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class ImageProcessor:
    def process(self, image_data: bytes, operations: list[dict]) -> bytes:
        try:
            from PIL import Image
        except ImportError:
            return image_data

        img = Image.open(BytesIO(image_data))
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
        fmt = img.format or "PNG"
        img.save(output, format=fmt)
        return output.getvalue()

    def get_info(self, image_data: bytes) -> dict:
        try:
            from PIL import Image
            img = Image.open(BytesIO(image_data))
            return {"width": img.size[0], "height": img.size[1], "format": img.format, "mode": img.mode}
        except Exception as e:
            return {"error": str(e)}
```

## Controller

```python
from pydantic import BaseModel
from lauren import controller, post, module, Bytes, Json

class ProcessRequest(BaseModel):
    operations: list[dict]

@controller("/images")
class ImageController:
    def __init__(self, processor: ImageProcessor) -> None:
        self._processor = processor

    @post("/process")
    async def process(self, file_data: Bytes) -> dict:
        # operations come from query or a separate endpoint in real apps
        result = self._processor.process(file_data, [{"op": "thumbnail", "width": 128, "height": 128}])
        return {"size": len(result)}

    @post("/info")
    async def info(self, file_data: Bytes) -> dict:
        return self._processor.get_info(file_data)

@module(controllers=[ImageController], providers=[ImageProcessor])
class ImageModule:
    pass
```

## Operations reference

| `op`        | Required keys                   | Notes                                 |
|-------------|---------------------------------|---------------------------------------|
| `resize`    | `width`, `height`               | Exact resize, may distort aspect ratio |
| `thumbnail` | `width`, `height`               | Preserves aspect ratio, fits in box   |
| `crop`      | `left`, `top`, `right`, `bottom`| Pixel coordinates                     |
| `grayscale` | —                               | Converts to single-channel `"L"` mode |

## Test helper

```python
def _make_test_image(width: int = 100, height: int = 100) -> bytes:
    from PIL import Image
    from io import BytesIO
    img = Image.new("RGB", (width, height), color="red")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
```

## Notes

- Install Pillow: `pip install Pillow`.
- For high-throughput pipelines consider `pillow-simd` or `libvips` (via
  `pyvips`) as drop-in accelerators.
- Run image processing in a thread pool via
  `anyio.to_thread.run_sync(processor.process, data, ops)` to avoid blocking
  the event loop on CPU-bound work.
