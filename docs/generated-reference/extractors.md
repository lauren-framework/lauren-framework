# Extractors

Typed extractors for decomposing HTTP requests into strongly-typed Python values.

## Path, Query, Header, Cookie

### `Path`

```python
class Path
```

### `Query`

```python
class Query
```

### `Header`

```python
class Header
```

### `Cookie`

```python
class Cookie
```

## Body extractors

### `Json`

```python
class Json
```

### `Form`

```python
class Form
```

### `Bytes`

```python
class Bytes
```

Raw bytes body extractor.

Use as ``body: Bytes`` — no type parameter required.

Buffers the entire body into a single ``bytes`` object before
handing it to the handler. For small requests this is exactly
what you want; for multi-megabyte uploads consider
:class:`ByteStream` instead, which yields the ASGI chunks directly
without an intermediate copy.

### `ByteStream`

```python
class ByteStream
```

Zero-copy streaming body extractor.

Use as ``body: ByteStream`` — the handler receives a
:class:`lauren.types.ByteStream` async iterator that yields each
ASGI body chunk as it arrives, without concatenating them into a
single ``bytes`` object.

Motivation
----------

The :class:`Bytes` extractor calls ``request.body()`` which eagerly
drains every ASGI ``http.request`` message into a ``list[bytes]``
and then joins them. For a 100 MiB upload that is ~200 MiB of
transient memory (the joined result plus the outstanding list of
chunks) plus the Python-level GC overhead of every intermediate
allocation.

``ByteStream`` skips the join entirely: it hands the handler an
async iterator that pulls chunks directly from the ASGI
``receive`` callable. The handler can pipe chunks into a file, a
hash function, or a network socket without ever holding the full
body in memory. Backpressure is preserved — each ``async for``
iteration only advances when the consumer is ready.

Example
-------

::

    @post("/upload")
    async def upload(self, body: ByteStream) -> dict:
        sha = hashlib.sha256()
        total = 0
        async for chunk in body:
            sha.update(chunk)
            total += len(chunk)
        return {"bytes": total, "sha256": sha.hexdigest()}

Safety
------

The body may only be consumed once — attempting to iterate the
same :class:`ByteStream` twice raises
:class:`ExtractorError`. This mirrors the single-shot nature of
ASGI ``receive``. Middleware that needs to inspect the body
should use :class:`Bytes` instead.

The framework still enforces the app's ``max_body_size`` across
the stream: if the cumulative chunk size exceeds the limit the
iterator raises :class:`RequestBodyTooLarge` — same behaviour as
the buffered ``request.body()`` path.

### `UploadFile`

```python
class UploadFile
```

Multipart file upload extractor — FastAPI-compatible ergonomics.

Declare a handler parameter as ``file: UploadFile`` and the
framework will parse the request's ``multipart/form-data`` body,
pick out the first part whose field name matches the parameter
name (or its ``alias`` if provided), and hand the handler a
:class:`lauren.types.UploadFile` instance with the file's bytes,
declared filename, content type, and headers.

Multiple uploads
----------------

For endpoints accepting several files in the same form, use the
list shape ``files: list[UploadFile]`` — the framework collects
every part with the matching field name into the list.

Example
-------

::

    @post("/avatar")
    async def upload(self, file: UploadFile) -> dict:
        return {
            "filename": file.filename,
            "content_type": file.content_type,
            "bytes": len(await file.read()),
        }

Limitations
-----------

* The full body is buffered before parsing. Very large uploads
  (hundreds of MiB) should use :class:`ByteStream` and implement
  chunked processing themselves.
* Nested ``multipart/mixed`` parts are not parsed.
* RFC 2231 parameter encoding for exotic filenames is not
  supported; plain and simple quoted-string names cover the
  modern browser and HTTP client output universe.

## Dependency injection extractor

### `Depends`

```python
class Depends
```

## Pipes

### `pipe`

```python
def pipe(target: PipeDecoratorTarget | None = None) -> Callable[[PipeDecoratorTarget], PipeDecoratorTarget] | PipeDecoratorTarget
```

Mark a function or class as a pipe.

Works in three interchangeable forms:

1. **Decorator factory** — ``@pipe()`` above a function or class::

       @pipe()
       def path_is_string(value, ctx):
           ...

       @pipe()
       class UserLookup:
           def transform(self, value, ctx):
               ...

2. **Inline helper** — ``pipe(existing_fn_or_cls)``::

       chain = PathField(ge=1) | pipe(validate_path) | path_is_string

3. **Bare decorator** — ``@pipe`` without parentheses is accepted too;
   since ``pipe`` performs the same thing whether called with or
   without parentheses there is no ambiguity.

Every form attaches :class:`PipeMeta` as ``target.__lauren_pipe__`` and
returns ``target`` unchanged. The attribute is idempotent: applying
:func:`pipe` twice is harmless.

``|`` composition on :class:`FieldDescriptor` / :class:`_ParamSpec`
then accepts any callable carrying this marker.

### `Pipe`

```python
class Pipe
```

Optional base class for NestJS-style class-based pipes.

Subclassing is purely cosmetic — the framework dispatches pipes by
looking for a ``transform(value, ctx)`` method and the
``__lauren_pipe__`` marker attribute. Use :func:`pipe` to attach that
marker::

    @pipe()
    class LookupUser(Pipe):
        def __init__(self, repo: UserRepo):
            self.repo = repo

        async def transform(self, value, ctx):
            return self.repo.get(value)

#### `Pipe.transform`

```python
def transform(self, value: object, ctx: PipeContext) -> object
```

### `PipeContext`

```python
class PipeContext(request: Request, name: str, source: str, inner_type: object, container: ResolverProtocol | None, request_cache: RequestCache | None, owning_module: type | None, field_descriptor: 'FieldDescriptor | None')
```

Context object passed to a pipe's transform function.

## Lower-level API

### `ExtractionMarker`

```python
class ExtractionMarker
```

Base class for extractor markers.

Built-in markers (``Path``, ``Query``, ``Json``, ...) use the ``source``
attribute for dispatch inside :func:`extract_parameter`. User-defined
extractors override the :meth:`extract` instance method to plug custom
extraction logic.

**Canonical form — instance method, DI optional:**

::

    from lauren.extractors import Extraction, ExtractionMarker
    from lauren.types import ExecutionContext

    class CurrentUser(ExtractionMarker):
        source = "current_user"  # any unique string

        async def extract(
            self,
            execution_context: ExecutionContext,
            extraction: Extraction,
        ) -> object:
            uid = execution_context.request.state.get("user_id")
            if uid is None:
                raise UnauthorizedError("not authenticated")
            return uid

The framework instantiates the extractor once with no arguments and
reuses the same instance across requests.  When the extractor needs
constructor dependencies, decorate it with ``@injectable`` and the DI
container will resolve and inject them::

    from lauren import injectable, Scope
    from lauren.extractors import Extraction, ExtractionMarker
    from lauren.types import ExecutionContext

    @injectable(scope=Scope.REQUEST)
    class CurrentUser(ExtractionMarker):
        source = "current_user"

        def __init__(self, repo: UserRepository) -> None:
            self._repo = repo

        async def extract(
            self,
            execution_context: ExecutionContext,
            extraction: Extraction,
        ) -> object:
            uid = execution_context.request.state.get("user_id")
            return await self._repo.get(uid)

The injectable form requires the extractor class to be listed in the
``providers=`` of at least one module in the DI graph.

**Legacy classmethod form (backward compat only):**

::

    class MyExtractor(ExtractionMarker):
        source = "legacy"

        @classmethod
        async def extract(cls, request, extraction, *, container, request_cache):
            ...

The classmethod form is still dispatched correctly but is superseded by
the instance method form above.

### `State`

```python
class State
```

### `FieldDescriptor`

```python
class FieldDescriptor(default: object = ..., alias: str | None = None, ge: float | None = None, le: float | None = None, gt: float | None = None, lt: float | None = None, min_length: int | None = None, max_length: int | None = None, pattern: str | None = None, description: str | None = None, example: object | None = None)
```

#### `FieldDescriptor.validate`

```python
def validate(self, name: str, value: V) -> V
```
