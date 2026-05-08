---
name: bulk-import-export
description: Imports and exports records as CSV, JSON, or Excel. Use when you need to process bulk data files uploaded by users or generate downloadable reports from database query results.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Bulk Import/Export (CSV / Excel / JSON) with Async Jobs

## Overview

`BulkProcessor` provides symmetric import/export for three formats using only
stdlib (`csv`, `json`) plus `openpyxl` for Excel. Pass it the raw bytes of an
uploaded file and get back a list of dicts; pass it a list of dicts and get
back bytes ready to stream to the client.

## BulkProcessor

```python
import csv
import io
import json
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class BulkProcessor:
    def import_csv(self, data: bytes, encoding: str = "utf-8") -> list[dict]:
        reader = csv.DictReader(io.StringIO(data.decode(encoding)))
        return list(reader)

    def export_csv(self, records: list[dict]) -> bytes:
        if not records:
            return b""
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
        return buf.getvalue().encode("utf-8")

    def import_json(self, data: bytes) -> list[dict]:
        return json.loads(data)

    def export_json(self, records: list[dict]) -> bytes:
        return json.dumps(records, indent=2).encode("utf-8")

    def import_excel(self, data: bytes) -> list[dict]:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data))
            ws = wb.active
            rows = list(ws.rows)
            if not rows:
                return []
            headers = [cell.value for cell in rows[0]]
            return [dict(zip(headers, [c.value for c in row])) for row in rows[1:]]
        except ImportError:
            return []

    def export_excel(self, records: list[dict]) -> bytes:
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            if records:
                ws.append(list(records[0].keys()))
                for r in records:
                    ws.append(list(r.values()))
            buf = io.BytesIO()
            wb.save(buf)
            return buf.getvalue()
        except ImportError:
            return b""
```

## Controller

```python
from lauren import controller, post, get, module, Bytes, Query
from lauren.types import Response

@controller("/bulk")
class BulkController:
    def __init__(self, processor: BulkProcessor) -> None:
        self._processor = processor

    @post("/import")
    async def import_data(self, body: Bytes, fmt: str = Query(default="csv")) -> dict:
        if fmt == "csv":
            records = self._processor.import_csv(body)
        elif fmt == "json":
            records = self._processor.import_json(body)
        elif fmt == "excel":
            records = self._processor.import_excel(body)
        else:
            from lauren.exceptions import BadRequestError
            raise BadRequestError(f"Unknown format: {fmt}")
        return {"count": len(records), "records": records}

    @get("/export")
    async def export_data(self, fmt: str = Query(default="csv")) -> Response:
        records = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        if fmt == "csv":
            data = self._processor.export_csv(records)
            media_type = "text/csv"
        elif fmt == "json":
            data = self._processor.export_json(records)
            media_type = "application/json"
        elif fmt == "excel":
            data = self._processor.export_excel(records)
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            from lauren.exceptions import BadRequestError
            raise BadRequestError(f"Unknown format: {fmt}")
        return Response(content=data, media_type=media_type)

@module(controllers=[BulkController], providers=[BulkProcessor])
class BulkModule:
    pass
```

## Async job pattern

For large files, offload processing to a background task so the HTTP response
returns immediately:

```python
from lauren.background import BackgroundTasks

@post("/import/async")
async def import_async(self, body: Bytes, tasks: BackgroundTasks) -> dict:
    job_id = str(uuid.uuid4())
    tasks.add_task(self._process_import, job_id, body)
    return {"job_id": job_id, "status": "queued"}

async def _process_import(self, job_id: str, data: bytes) -> None:
    records = self._processor.import_csv(data)
    # persist to DB, update job status, etc.
```

## Notes

- `openpyxl` is required for Excel: `pip install openpyxl`.
- For very large CSVs use streaming with `csv.reader` over a chunked upload
  rather than loading the whole file into memory.
- Excel `cell.value` returns Python native types (int, float, datetime) — cast
  to str when a uniform `dict[str, str]` is needed downstream.
