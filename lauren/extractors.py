"""Extractors — convert ASGI request data into typed Python values.

Extractors are declared using ``Annotated[Type, MARKER]`` or via the
specialized generic aliases ``Path[T]``, ``Query[T]``, ``Json[T]`` etc. At
startup the handler signature is introspected and extractors are resolved into
an immutable plan executed per-request.

Placement rules
---------------

* **Extractor markers** (``Path``, ``Query``, ``Header``, ``Cookie``, ``Json``,
  ``Form``, ``Bytes``, ``Depends``, ``State``, and user-defined subclasses of
  :class:`ExtractionMarker`) declare the *source* of a parameter's value and
  must appear in the type annotation. They never appear as defaults.

* **Field descriptors** (``PathField``, ``QueryField``, ``HeaderField``,
  ``CookieField``) and **pipes** (:func:`pipe`) are *behavioural*: they
  validate, re-alias, and transform the extracted value. They may appear
  either inside ``Annotated[...]`` alongside the extractor marker **or** as
  the parameter default (composed with ``&``) — whichever reads better.

  ::

      # Both forms are equivalent:
      def a(self, id: Annotated[Path[int], PathField(ge=1), pipe(lookup)]): ...
      def b(self, id: Path[int] = PathField(ge=1) & pipe(lookup)): ...
"""

from __future__ import annotations

import inspect as _inspect
import json as jsonlib
import types as _types
from dataclasses import dataclass
from typing import (
    Annotated,
    Any,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from .exceptions import ExtractorError, ExtractorFieldError, MissingProviderError
from .types import Request

try:
    import pydantic

    _PYDANTIC_AVAILABLE = True
    _BaseModel = pydantic.BaseModel
except ImportError:  # pragma: no cover
    _PYDANTIC_AVAILABLE = False
    _BaseModel = None  # type: ignore[assignment,misc]

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Extractor markers — subscriptable generic sentinels.
# ---------------------------------------------------------------------------


class ExtractionMarker:
    """Base class for extractor markers.

    Built-in markers (``Path``, ``Query``, ``Json``, ...) use the ``source``
    attribute for dispatch inside :func:`extract_parameter`. User-defined
    extractors can instead override the :meth:`extract` classmethod **or**
    define an instance method combined with ``@injectable`` to plug
    custom extraction logic without modifying the framework.

    **Classic form — classmethod with explicit container access:**

    ::

        from lauren import DIContainer
        from lauren.types import Request
        from lauren.extractors import Extraction

        class CurrentUser(ExtractionMarker):
            source = "current_user"  # any unique string

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: DIContainer,
                request_cache: dict[type, object] | None,
            ) -> object:
                # manually resolve deps from the container when needed
                repo = await container.resolve(UserRepository, request_cache=request_cache)
                ...

    **Injectable form — constructor DI, simpler extract signature:**

    When the marker class is decorated with ``@injectable``, define
    ``extract`` as a plain instance method. Lauren will resolve the instance
    via the DI container so all constructor dependencies are available with
    no manual ``container.resolve()`` calls inside ``extract``::

        from lauren import injectable, Scope
        from lauren.types import Request
        from lauren.extractors import Extraction

        @injectable(scope=Scope.SINGLETON)
        class CurrentUser(ExtractionMarker):
            source = "current_user"

            def __init__(self, repo: UserRepository) -> None:
                self._repo = repo

            async def extract(
                self,
                request: Request,
                extraction: Extraction,
            ) -> object:
                # deps are already available via self._repo
                ...

    The injectable form requires the extractor class to be listed in the
    ``providers=`` of at least one module in the DI graph.
    """

    source: str = "unknown"
    reads_body: bool = False

    def __class_getitem__(cls, item: Any) -> Any:
        # Returns ``Annotated[item, marker_instance]`` so user code can write
        # ``user_id: Path[int]`` and we can detect it in type hints.
        return Annotated[item, cls]  # type: ignore[valid-type]

    # ------------------------------------------------------------------
    # Custom extractor hook. Subclasses may override this as either:
    #   • a @classmethod — legacy, receives container/request_cache kwargs
    #   • an instance method — only valid when the class is @injectable;
    #     constructor deps are resolved by the DI container automatically.
    #
    # NOTE: intentionally not defined on the base class so that
    # ``hasattr(marker, 'extract')`` reliably distinguishes custom markers.
    # ------------------------------------------------------------------


class Path(ExtractionMarker):
    source = "path"


class Query(ExtractionMarker):
    source = "query"


class Header(ExtractionMarker):
    source = "header"


class Cookie(ExtractionMarker):
    source = "cookie"


class Json(ExtractionMarker):
    source = "json"
    reads_body = True


class Form(ExtractionMarker):
    source = "form"
    reads_body = True


class State(ExtractionMarker):
    source = "state"


class Depends(ExtractionMarker):
    source = "depends"


class Bytes(ExtractionMarker):
    """Raw bytes body extractor.

    Use as ``body: Bytes`` — no type parameter required.

    Buffers the entire body into a single ``bytes`` object before
    handing it to the handler. For small requests this is exactly
    what you want; for multi-megabyte uploads consider
    :class:`ByteStream` instead, which yields the ASGI chunks directly
    without an intermediate copy.
    """

    source = "bytes"
    reads_body = True


class UploadFile(ExtractionMarker):
    """Multipart file upload extractor — FastAPI-compatible ergonomics.

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
    """

    source = "upload_file"
    reads_body = True


class ByteStream(ExtractionMarker):
    """Zero-copy streaming body extractor.

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
    """

    source = "byte_stream"
    reads_body = True


# ---------------------------------------------------------------------------
# Field descriptor — for validation metadata.
# ---------------------------------------------------------------------------


@dataclass
class FieldDescriptor:
    default: Any = ...
    alias: str | None = None
    ge: float | None = None
    le: float | None = None
    gt: float | None = None
    lt: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    description: str | None = None
    example: Any = None

    # ------------------------------------------------------------------
    # Composition — ``PathField(...) | pipe(fn) | MyPipeClass`` builds a
    # chain of a single ``FieldDescriptor`` plus an ordered list of
    # ``__lauren_pipe__``-marked callables.
    # ------------------------------------------------------------------

    def __or__(self, other: Any) -> "_ParamSpec":
        return _ParamSpec(field_descriptor=self) | other

    __ror__ = __or__

    def validate(self, name: str, value: Any) -> Any:
        if value is None and self.default is not ...:
            return self.default
        if isinstance(value, (int, float)):
            if self.ge is not None and value < self.ge:
                raise ExtractorFieldError(
                    f"{name} must be >= {self.ge}",
                    detail={"field": name, "constraint": "ge"},
                )
            if self.le is not None and value > self.le:
                raise ExtractorFieldError(
                    f"{name} must be <= {self.le}",
                    detail={"field": name, "constraint": "le"},
                )
            if self.gt is not None and value <= self.gt:
                raise ExtractorFieldError(
                    f"{name} must be > {self.gt}",
                    detail={"field": name, "constraint": "gt"},
                )
            if self.lt is not None and value >= self.lt:
                raise ExtractorFieldError(
                    f"{name} must be < {self.lt}",
                    detail={"field": name, "constraint": "lt"},
                )
        if isinstance(value, str):
            if self.min_length is not None and len(value) < self.min_length:
                raise ExtractorFieldError(f"{name} too short", detail={"field": name})
            if self.max_length is not None and len(value) > self.max_length:
                raise ExtractorFieldError(f"{name} too long", detail={"field": name})
            if self.pattern is not None:
                import re

                if not re.fullmatch(self.pattern, value):
                    raise ExtractorFieldError(
                        f"{name} does not match pattern",
                        detail={"field": name, "pattern": self.pattern},
                    )
        return value


def PathField(**kwargs: Any) -> FieldDescriptor:
    return FieldDescriptor(**kwargs)


def QueryField(**kwargs: Any) -> FieldDescriptor:
    return FieldDescriptor(**kwargs)


def HeaderField(**kwargs: Any) -> FieldDescriptor:
    return FieldDescriptor(**kwargs)


def CookieField(**kwargs: Any) -> FieldDescriptor:
    return FieldDescriptor(**kwargs)


# ---------------------------------------------------------------------------
# Pipes — Axum/NestJS-style layered validation & transformation.
# ---------------------------------------------------------------------------


PIPE_META = "__lauren_pipe__"


@dataclass
class PipeMeta:
    """Marker metadata attached to any callable acting as a pipe.

    The attribute lives under ``target.__lauren_pipe__`` and follows the
    same marker-attribute convention as every other lauren decorator
    (``@controller`` → ``__lauren_controller__``, ``@injectable`` →
    ``__lauren_injectable__``, etc.).

    Carrying metadata as an attribute (rather than wrapping the original
    callable in an opaque object) keeps the function callable as a
    function, the class callable as a class, and allows static tools and
    ``inspect`` to see the untouched signature. The framework locates
    pipes by looking for ``PIPE_META`` at plan-compilation time.
    """

    #: The callable or class the marker was attached to. ``target`` is
    #: kept even though ``target.__lauren_pipe__ is self`` so that future
    #: versions can carry additional fields (description, scope, etc.)
    #: without changing the public shape.
    target: Any


@dataclass
class PipeContext:
    """Context object passed to a pipe's transform function."""

    #: The live :class:`Request` being processed.
    request: Request
    #: Name of the handler parameter being transformed.
    name: str
    #: Source of the original value (``"path"``, ``"query"``, ``"json"``,
    #: ``"depends"``, etc.) — useful for shared pipes that behave differently
    #: depending on where the value came from.
    source: str
    #: Python type declared inside the extractor marker (e.g. ``int`` for
    #: ``Path[int]``).
    inner_type: Any
    #: The DI container — lets a pipe resolve services on demand.
    container: Any
    #: The per-request DI cache, forwarded unchanged.
    request_cache: dict[type, Any] | None
    #: The module class declaring the controller (for DI visibility).
    owning_module: type | None
    #: The field descriptor attached to the parameter, if any.
    field_descriptor: "FieldDescriptor | None"


class Pipe:
    """Optional base class for NestJS-style class-based pipes.

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
    """

    async def transform(self, value: Any, ctx: PipeContext) -> Any:
        raise NotImplementedError


def _mark_as_pipe(target: Any) -> Any:
    """Attach :class:`PipeMeta` to ``target`` and return it unchanged.

    Idempotent — re-marking is a no-op. Raises :class:`TypeError` if the
    target isn't a class or callable, so mistakes like ``pipe(42)`` are
    caught immediately at the decoration site.
    """
    if not (isinstance(target, type) or callable(target)):
        raise TypeError(
            f"@pipe() can only decorate a function or class, got {type(target).__name__}.",
        )
    try:
        existing = (
            target.__dict__.get(PIPE_META) if hasattr(target, "__dict__") else None
        )
    except Exception:  # pragma: no cover - extremely exotic targets
        existing = None
    if existing is None:
        try:
            setattr(target, PIPE_META, PipeMeta(target=target))
        except (AttributeError, TypeError) as exc:
            raise TypeError(
                f"Cannot mark {target!r} as a pipe: "
                "the target does not allow custom attributes. "
                "Wrap it in a small function or class and decorate that instead."
            ) from exc
    return target


def pipe(target: Any = None) -> Any:
    """Mark a function or class as a pipe.

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
    """
    if target is None:
        # ``@pipe()`` — return a decorator.
        def decorator(obj: Any) -> Any:
            return _mark_as_pipe(obj)

        return decorator
    # ``pipe(target)`` / ``@pipe`` (bare) — mark and return.
    return _mark_as_pipe(target)


def is_pipe(obj: Any) -> bool:
    """Return True if ``obj`` carries the ``__lauren_pipe__`` marker."""
    return getattr(obj, PIPE_META, None) is not None


@dataclass
class _ParamSpec:
    """Composite parameter-level default produced by ``|`` composition.

    Holds an optional :class:`FieldDescriptor` plus an ordered tuple of
    pipe callables (each of which carries ``__lauren_pipe__``). Accepted
    anywhere a ``FieldDescriptor`` default would be; the compiler
    (:func:`_compile_handler_signature`) pulls the pieces out and attaches
    them to the :class:`Extraction`.
    """

    field_descriptor: FieldDescriptor | None = None
    pipes: tuple[Any, ...] = ()

    @property
    def default(self) -> Any:
        """Expose the underlying :class:`FieldDescriptor` default (if any)
        so ``_ParamSpec`` remains a drop-in replacement for a bare
        ``FieldDescriptor`` default. Returns ``...`` when no descriptor
        was included in the chain."""
        return self.field_descriptor.default if self.field_descriptor else ...

    def __or__(self, other: Any) -> "_ParamSpec":
        if isinstance(other, _ParamSpec):
            # Reject ambiguous conflicts — two descriptors in one chain.
            if self.field_descriptor is not None and other.field_descriptor is not None:
                raise TypeError(
                    "A parameter chain may include at most one FieldDescriptor; "
                    "combine the constraints into a single PathField / QueryField call."
                )
            fd = other.field_descriptor or self.field_descriptor
            return _ParamSpec(
                field_descriptor=fd,
                pipes=self.pipes + other.pipes,
            )
        if isinstance(other, FieldDescriptor):
            if self.field_descriptor is not None:
                raise TypeError(
                    "A parameter chain may include at most one FieldDescriptor."
                )
            return _ParamSpec(field_descriptor=other, pipes=self.pipes)
        if is_pipe(other):
            return _ParamSpec(
                field_descriptor=self.field_descriptor,
                pipes=self.pipes + (other,),
            )
        # Callable but un-marked — point the user at the right incantation.
        if callable(other) or isinstance(other, type):
            name = getattr(other, "__name__", repr(other))
            raise TypeError(
                f"{name} is not marked as a pipe. "
                f"Decorate it with @pipe() or wrap it inline with pipe({name}) "
                "before composing it into a | chain."
            )
        return NotImplemented

    __ror__ = __or__


# ---------------------------------------------------------------------------
# Extractor plan compilation + execution.
# ---------------------------------------------------------------------------


@dataclass
class Extraction:
    """A single parameter extraction step."""

    name: str
    source: str
    inner_type: Any
    field_descriptor: FieldDescriptor | None
    default: Any
    has_default: bool
    reads_body: bool = False
    #: The marker class, preserved when a custom extractor is in play.
    marker_cls: type | None = None
    #: Ordered tuple of pipe callables applied to the extracted value in
    #: the order they appear (annotation metadata first, then default-side).
    #: Each entry carries the ``__lauren_pipe__`` marker attached by
    #: :func:`pipe`. An empty tuple means "no pipes" — the common case.
    pipes: tuple[Any, ...] = ()


def _peel_optional(annotation: Any) -> tuple[Any, bool]:
    """Strip a ``None`` branch off a ``Union`` / PEP 604 union.

    Returns ``(unwrapped, is_optional)`` where ``unwrapped`` is the
    annotation with the ``NoneType`` branch removed, and ``is_optional``
    reports whether a ``None`` branch was present. If the annotation is
    not an optional-shaped union, ``unwrapped`` is the input unchanged
    and ``is_optional`` is ``False``.

    Handles three source shapes:

    * ``typing.Optional[T]`` / ``Union[T, None]`` — ``get_origin`` is
      ``typing.Union``.
    * PEP 604 ``T | None`` — ``isinstance(ann, types.UnionType)``.
    * Any other shape — returned as-is.

    Two-way unions like ``Union[A, B, None]`` collapse to
    ``Union[A, B]`` when rebuilt; single-branch cases collapse to the
    lone non-None branch. The helper never raises — callers downstream
    decide whether the resulting shape is a valid extractor target.
    """
    origin = get_origin(annotation)
    is_pep604 = isinstance(annotation, _types.UnionType)
    if origin is not Union and not is_pep604:
        return annotation, False
    args = [a for a in get_args(annotation) if a is not type(None)]
    if len(args) == len(get_args(annotation)):
        return annotation, False  # no None branch
    if not args:
        # ``Union[None]`` — degenerate but harmless; treat as optional
        # with unknown inner.
        return type(None), True
    if len(args) == 1:
        return args[0], True
    # Multi-branch union minus None. Rebuild preserving the original
    # spelling so ``Union[A, B, None]`` becomes ``Union[A, B]``.
    if is_pep604:
        rebuilt = args[0]
        for a in args[1:]:
            rebuilt = rebuilt | a
        return rebuilt, True
    return Union[tuple(args)], True  # type: ignore[valid-type]


def parse_extractor_hint(
    annotation: Any,
) -> tuple[
    str | None,
    Any,
    bool,
    type | None,
    FieldDescriptor | None,
    tuple[Any, ...],
]:
    """Inspect an annotation and return its extractor metadata.

    Returns a 6-tuple ``(source, inner_type, reads_body, marker_cls,
    field_descriptor, pipes)``. Any of the last two entries may be ``None``
    / empty when the annotation doesn't carry the extra metadata.

    Recognised ``Annotated`` metadata items:

    * An :class:`ExtractionMarker` (class or instance) — picks the source.
    * A :class:`FieldDescriptor` — contributes validation/aliasing.
    * Anything carrying ``__lauren_pipe__`` — appended to the pipe chain
      in declaration order (produced by :func:`pipe` or ``@pipe()``).
    * A :class:`_ParamSpec` (produced by a ``|`` chain) — expanded inline.

    **Optional wrapping.** Two shapes are recognised:

    * ``Optional[Path[int]]`` / ``Path[int] | None`` — the outer union
      is stripped and the parser recurses into ``Path[int]``. A
      :class:`FieldDescriptor` with ``default=None`` is synthesised so
      a missing value produces ``None`` instead of raising
      :class:`ExtractorFieldError`.
    * ``Path[Optional[int]]`` — the inner type becomes ``Optional[int]``
      and scalar coercion threads the ``None`` value through
      automatically. The parameter is also treated as nullable (a
      missing path/query/header value resolves to ``None``).

    Unrecognised items are ignored so external type-checker helpers coexist
    peacefully.
    """
    # ``Optional[Extractor[T]]`` / ``Extractor[T] | None`` — unwrap the
    # outer union and recurse. We synthesise an optional FieldDescriptor
    # (default=None) so the rest of the pipeline treats missing values
    # as ``None`` rather than raising.
    peeled, outer_optional = _peel_optional(annotation)
    if outer_optional and peeled is not annotation:
        (
            inner_src,
            inner_inner,
            inner_reads,
            inner_marker,
            inner_fd,
            inner_pipes,
        ) = parse_extractor_hint(peeled)
        if inner_src is not None:
            # Merge a None-default into the descriptor so the
            # extraction step returns None on missing values.
            if inner_fd is None:
                inner_fd = FieldDescriptor(default=None)
            elif inner_fd.default is ...:
                inner_fd = _dc_replace_default(inner_fd, None)
            return (
                inner_src,
                inner_inner,
                inner_reads,
                inner_marker,
                inner_fd,
                inner_pipes,
            )
    origin = get_origin(annotation)
    pipes: list[Any] = []
    fd: FieldDescriptor | None = None
    if origin is Annotated or (
        hasattr(annotation, "__metadata__") and hasattr(annotation, "__origin__")
    ):
        args = get_args(annotation)
        inner = args[0]
        source: str | None = None
        reads_body = False
        marker_cls: type | None = None
        # Pydantic ``FieldInfo`` with a ``discriminator=...`` must ride
        # along with the inner type so the JSON validator / streaming
        # reader can still detect the tagged union after this parser
        # has unwrapped the outer Annotated[...] (feature 6).
        preserved_metadata: list[Any] = []
        # If the inner is itself an ``Annotated`` (e.g. ``Path[int]`` is
        # ``Annotated[int, Path]``) we recurse so users can write
        # ``Annotated[Path[int], pipe(...), PathField(...)]`` without losing
        # the marker.
        (
            inner_src,
            inner_inner,
            inner_reads,
            inner_marker,
            inner_fd,
            inner_pipes,
        ) = parse_extractor_hint(inner)
        if inner_src is not None:
            source, inner, reads_body, marker_cls = (
                inner_src,
                inner_inner,
                inner_reads,
                inner_marker,
            )
            if inner_fd is not None:
                fd = inner_fd
            pipes.extend(inner_pipes)
        for extra in args[1:]:
            # Extractor marker (class or instance).
            candidate = extra if isinstance(extra, type) else type(extra)
            if (
                isinstance(candidate, type)
                and issubclass(candidate, ExtractionMarker)
                and source is None
            ):
                source = candidate.source
                reads_body = candidate.reads_body
                marker_cls = candidate
                continue
            if isinstance(extra, FieldDescriptor):
                if fd is not None:
                    raise ExtractorError(
                        "multiple FieldDescriptor entries in Annotated[...]; "
                        "keep at most one per parameter",
                    )
                fd = extra
                continue
            if is_pipe(extra):
                pipes.append(extra)
                continue
            if isinstance(extra, _ParamSpec):
                # Someone passed a ``PathField(...) & pipe(...)`` chain
                # directly into Annotated. Expand it.
                if extra.field_descriptor is not None:
                    if fd is not None:
                        raise ExtractorError(
                            "multiple FieldDescriptor entries in Annotated[...]",
                        )
                    fd = extra.field_descriptor
                pipes.extend(extra.pipes)
                continue
            # Pydantic ``FieldInfo`` with a ``discriminator`` — keep so
            # tagged-union detection still works downstream (feature 6).
            if _is_discriminator_fieldinfo(extra):
                preserved_metadata.append(extra)
                continue
        if source is not None:
            # Re-wrap the inner type so the preserved FieldInfo stays
            # attached to it. Without this step, ``Json[Annotated[Union[A,B],
            # Field(discriminator='kind')]]`` would lose its discriminator
            # metadata and the JSON validator would see a bare ``Union``.
            if preserved_metadata:
                inner = Annotated[(inner, *preserved_metadata)]  # type: ignore[valid-type]
            return source, inner, reads_body, marker_cls, fd, tuple(pipes)
    # Bare marker class used as type (e.g. ``body: Bytes`` or ``user: CurrentUser``).
    if isinstance(annotation, type) and issubclass(annotation, ExtractionMarker):
        return (
            annotation.source,
            annotation,
            annotation.reads_body,
            annotation,
            None,
            (),
        )
    # ``list[UploadFile]`` / ``tuple[UploadFile, ...]`` shape: recognise
    # the container-of-marker pattern so handlers can accept multiple
    # parts sharing a field name. The inner type is preserved as-is so
    # the extraction layer can detect the list shape and collect every
    # matching part rather than returning just the first one.
    if get_origin(annotation) in (list, tuple):
        args = get_args(annotation)
        if args and isinstance(args[0], type) and issubclass(args[0], ExtractionMarker):
            marker = args[0]
            return (
                marker.source,
                annotation,
                marker.reads_body,
                marker,
                None,
                (),
            )
    return None, annotation, False, None, None, ()


def _is_pydantic_model_type(annotation: Any) -> bool:
    """Return ``True`` when *annotation* (possibly ``Optional[T]``) is a Pydantic model.

    Used by :func:`lauren._asgi._compile_handler_signature` to auto-promote
    bare model parameters to JSON body extraction.  Always returns ``False``
    when pydantic is not installed.
    """
    if _BaseModel is None:
        return False
    inner, _ = _peel_optional(annotation)
    return isinstance(inner, type) and issubclass(inner, _BaseModel)


#: Primitive Python types that can be meaningfully coerced from a query-string
#: segment without any DI or body-parsing machinery.
_SCALAR_TYPES: frozenset[type] = frozenset({int, float, str, bool, bytes, complex})


def _is_implicit_query_type(annotation: Any) -> bool:
    """Return ``True`` when *annotation* should be auto-promoted to a query param.

    Recognised shapes (all can optionally be wrapped in ``Optional[...]``
    / ``T | None``):

    * Any member of :data:`_SCALAR_TYPES` (``int``, ``float``, ``str``,
      ``bool``, ``bytes``, ``complex``).
    * ``list[scalar]`` / ``tuple[scalar, ...]`` — multi-value query params.

    Note: bare ``inspect.Parameter.empty`` (no annotation at all) intentionally
    returns ``False`` so that completely unannotated parameters still raise
    :class:`~lauren.exceptions.UnresolvableParameterError` at startup.
    An annotation of ``str`` must be written explicitly.

    Deliberately narrow so that unregistered DI tokens (protocols, services)
    and multi-binding ``list[Service]`` patterns still fail loudly at startup
    rather than silently becoming empty query parameters.
    """
    import inspect as _inspect

    if annotation is _inspect.Parameter.empty:
        return False
    inner, _ = _peel_optional(annotation)
    if inner in _SCALAR_TYPES:
        return True
    origin = get_origin(inner)
    if origin in (list, tuple):
        args = get_args(inner)
        if args and args[0] in _SCALAR_TYPES:
            return True
    return False


def _is_discriminator_fieldinfo(obj: Any) -> bool:
    """Detect a ``pydantic.Field(discriminator=...)`` metadata entry.

    Keeps the check attribute-based so the module stays importable when
    pydantic is absent — matches the pattern used by
    :mod:`lauren.streaming`.
    """
    disc = getattr(obj, "discriminator", None)
    return isinstance(disc, str) and bool(disc)


def _dc_replace_default(fd: "FieldDescriptor", new_default: Any) -> "FieldDescriptor":
    """Return a copy of ``fd`` with the ``default`` slot replaced.

    Used when synthesising an optional descriptor on an ``Optional[Path[T]]``
    annotation: the user may have supplied constraints (``ge``, ``le``,
    ``pattern``) without a default, in which case we keep those
    constraints and simply plug a ``None`` default on top so missing
    values don't raise.
    """
    from dataclasses import replace as _replace

    return _replace(fd, default=new_default)


def _coerce_scalar(value: str, target: Any) -> Any:
    """Coerce a raw string value (from a path / query / header) to
    ``target``.

    Supports scalar primitives, ``list[T]``/``tuple[T, ...]``, and
    nullable shapes:

    * ``Optional[int]`` / ``int | None`` — coerce to the underlying
      type; the nullable wrapping only matters when the value is
      missing (handled at the extraction layer, not here).
    * ``Union[A, B]`` without ``None`` — attempted in declaration
      order; the first successful coercion wins.

    A ``None`` input short-circuits to ``None`` regardless of target.
    """
    if value is None:
        return None
    # Peel ``Optional`` so ``Path[int | None]`` coerces through ``int``.
    target, _ = _peel_optional(target)
    if target is str or target is Any:
        return value
    if target is int:
        try:
            return int(value)
        except ValueError as e:
            raise ExtractorFieldError(str(e)) from e
    if target is float:
        try:
            return float(value)
        except ValueError as e:
            raise ExtractorFieldError(str(e)) from e
    if target is bool:
        return value.lower() in ("1", "true", "yes", "on")
    # list[T]
    origin = get_origin(target)
    if origin in (list, tuple):
        args = get_args(target)
        elem_t = args[0] if args else str
        return [_coerce_scalar(v, elem_t) for v in value.split(",")]
    # Non-optional union — try each branch in order. First success
    # wins; if every branch fails, raise the last failure.
    if origin is Union or isinstance(target, _types.UnionType):
        last_error: Exception | None = None
        for branch in get_args(target):
            if branch is type(None):
                continue
            try:
                return _coerce_scalar(value, branch)
            except ExtractorFieldError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
    return value


async def _run_pipes(
    value: Any,
    extraction: Extraction,
    *,
    request: Request,
    container: Any,
    request_cache: dict[type, Any] | None,
    owning_module: type | None,
) -> Any:
    """Apply every pipe on ``extraction`` to ``value`` in order."""
    if not extraction.pipes:
        return value
    ctx = PipeContext(
        request=request,
        name=extraction.name,
        source=extraction.source,
        inner_type=extraction.inner_type,
        container=container,
        request_cache=request_cache,
        owning_module=owning_module,
        field_descriptor=extraction.field_descriptor,
    )
    current = value
    for p in extraction.pipes:
        current = await _invoke_pipe(p, current, ctx)
    return current


#: Cache of no-arg instances for class-based pipes that aren't registered
#: with the DI container. Keyed by the class object itself; stays tiny
#: because it only grows with the number of distinct pipe classes declared
#: across the whole application.
_PIPE_INSTANCE_CACHE: dict[type, Any] = {}


async def _invoke_pipe(target: Any, value: Any, ctx: PipeContext) -> Any:
    """Run a single pipe, resolving its transform callable lazily.

    Supported shapes for ``target``:

    * a plain function — called with ``(value, ctx)`` or ``(value,)``,
    * a class with an ``@injectable`` registration — resolved via DI so
      module-scoped visibility applies,
    * a class without DI registration — instantiated once (process-wide
      cache) and invoked via its ``transform`` method.

    The ``target`` is always a callable carrying ``__lauren_pipe__`` —
    chain composition refuses to accept anything else.
    """
    # Class-based pipe: resolve via DI when visible, else instantiate.
    if _inspect.isclass(target):
        if ctx.container is not None and ctx.container.has_provider(
            target, owning_module=ctx.owning_module
        ):
            instance = await ctx.container.resolve(
                target,
                request_cache=ctx.request_cache,
                framework_values={Request: ctx.request, type(ctx.request): ctx.request},
                owning_module=ctx.owning_module,
            )
        else:
            instance = _PIPE_INSTANCE_CACHE.get(target)
            if instance is None:
                instance = target()
                _PIPE_INSTANCE_CACHE[target] = instance
        if not hasattr(instance, "transform"):
            raise ExtractorError(
                f"pipe target {target.__name__} must define 'transform(value, ctx)'",
                detail={"field": ctx.name, "pipe": target.__name__},
            )
        fn = instance.transform
    else:
        fn = target

    # Dispatch based on arity — single-arg callables get just the value.
    try:
        sig = _inspect.signature(fn)
        params = [
            pp
            for pp in sig.parameters.values()
            if pp.kind
            not in (
                _inspect.Parameter.VAR_POSITIONAL,
                _inspect.Parameter.VAR_KEYWORD,
            )
        ]
        arity = len(params)
    except (TypeError, ValueError):
        arity = 2  # unknown — pass both and let it raise if wrong.

    try:
        if arity <= 1:
            result = fn(value)
        else:
            result = fn(value, ctx)
    except ExtractorError:
        raise
    except Exception as exc:
        pipe_name = getattr(target, "__name__", repr(target))
        raise ExtractorError(
            f"pipe {pipe_name} failed: {exc}",
            detail={"field": ctx.name, "pipe": pipe_name},
        ) from exc
    if _inspect.isawaitable(result):
        result = await result
    return result


async def extract_parameter(
    request: Request,
    extraction: Extraction,
    *,
    container: Any = None,
    request_cache: dict[type, Any] | None = None,
    owning_module: type | None = None,
) -> Any:
    """Execute a single extraction. Raises :class:`ExtractorError` on failure.

    ``owning_module`` is the module class that declared the controller whose
    endpoint is being invoked. It is forwarded to the DI container so that
    ``Depends[X]`` resolution honours module visibility.

    After the source-specific extraction (and any ``FieldDescriptor``
    validation) the value flows through every pipe on the extraction, in
    declaration order. Pipes may freely replace, re-type, or side-effect
    the value.
    """
    value = await _extract_raw(
        request,
        extraction,
        container=container,
        request_cache=request_cache,
        owning_module=owning_module,
    )
    return await _run_pipes(
        value,
        extraction,
        request=request,
        container=container,
        request_cache=request_cache,
        owning_module=owning_module,
    )


async def _extract_raw(
    request: Request,
    extraction: Extraction,
    *,
    container: Any = None,
    request_cache: dict[type, Any] | None = None,
    owning_module: type | None = None,
) -> Any:
    """Source-specific extraction without pipe application."""
    source = extraction.source
    inner = extraction.inner_type
    fd = extraction.field_descriptor

    try:
        if source == "path":
            raw = request.path_params.get(extraction.name)
            if raw is None:
                if extraction.has_default:
                    return extraction.default
                if fd and fd.default is not ...:
                    # ``Optional[Path[T]]`` / ``Path[T] | None`` inject
                    # a ``FieldDescriptor(default=None)`` at parse
                    # time; honour it so missing nullable path params
                    # resolve to ``None`` instead of raising.
                    return fd.default
                raise ExtractorFieldError(
                    f"missing path parameter {extraction.name!r}",
                    detail={"field": extraction.name},
                )
            value = _coerce_scalar(raw, inner)
            return fd.validate(extraction.name, value) if fd else value

        if source == "query":
            # Pydantic model from query string: collect individual fields
            # by name so ``Query[Filters]`` works as an inline collection of
            # query params rather than looking for a single ``?filters=…``
            # key.  Pydantic handles type coercion when validating the dict.
            peeled_inner, _inner_opt = _peel_optional(inner)
            if (
                _PYDANTIC_AVAILABLE
                and isinstance(peeled_inner, type)
                and issubclass(peeled_inner, _BaseModel)
            ):
                fields_dict: dict[str, Any] = {}
                for f_name, f_info in peeled_inner.model_fields.items():
                    f_alias = (
                        f_info.alias  # type: ignore[attr-defined]
                        if getattr(f_info, "alias", None)
                        else f_name
                    )
                    raw = request.query_params.get(
                        f_alias, request.query_params.get(f_name, [])
                    )
                    if raw:
                        fields_dict[f_name] = raw[0] if len(raw) == 1 else raw
                if not fields_dict and _inner_opt:
                    if extraction.has_default:
                        return extraction.default
                    return None
                return _validate_pydantic(fields_dict, peeled_inner, extraction.name)

            key = fd.alias if fd and fd.alias else extraction.name
            raw_list = request.query_params.get(key, [])
            origin = get_origin(inner)
            if origin in (list, tuple):
                elem_t = get_args(inner)[0] if get_args(inner) else str
                values = [_coerce_scalar(v, elem_t) for v in raw_list]
                if fd:
                    values = fd.validate(extraction.name, values)
                return values
            if not raw_list:
                if extraction.has_default:
                    return extraction.default
                if fd and fd.default is not ...:
                    return fd.default
                raise ExtractorFieldError(
                    f"missing query parameter {key!r}",
                    detail={"field": key},
                )
            value = _coerce_scalar(raw_list[0], inner)
            return fd.validate(extraction.name, value) if fd else value

        if source == "header":
            key = fd.alias if fd and fd.alias else extraction.name.replace("_", "-")
            raw = request.headers.get(key)
            if raw is None:
                if extraction.has_default:
                    return extraction.default
                if fd and fd.default is not ...:
                    return fd.default
                raise ExtractorFieldError(
                    f"missing header {key!r}", detail={"field": key}
                )
            value = _coerce_scalar(raw, inner)
            return fd.validate(extraction.name, value) if fd else value

        if source == "cookie":
            key = fd.alias if fd and fd.alias else extraction.name
            raw = request.cookies.get(key)
            if raw is None:
                if extraction.has_default:
                    return extraction.default
                if fd and fd.default is not ...:
                    return fd.default
                raise ExtractorFieldError(
                    f"missing cookie {key!r}", detail={"field": key}
                )
            value = _coerce_scalar(raw, inner)
            return fd.validate(extraction.name, value) if fd else value

        if source == "json":
            body = await request.body()
            if not body:
                if extraction.has_default:
                    return extraction.default
                raise ExtractorFieldError(
                    "missing JSON body", detail={"field": extraction.name}
                )
            try:
                data = jsonlib.loads(body)
            except jsonlib.JSONDecodeError as e:
                raise ExtractorError(
                    f"invalid JSON: {e}", detail={"field": extraction.name}
                ) from e
            return _validate_json(data, inner, extraction.name)

        if source == "form":
            form_data = await request.form()
            if (
                _PYDANTIC_AVAILABLE
                and isinstance(inner, type)
                and issubclass(inner, _BaseModel)
            ):
                flat = {k: v[0] if len(v) == 1 else v for k, v in form_data.items()}
                return _validate_pydantic(flat, inner, extraction.name)
            return form_data

        if source == "bytes":
            return await request.body()

        if source == "byte_stream":
            # Lazy import to avoid a circular import at module load.
            from .types import ByteStream as _ByteStream

            return _ByteStream(request)

        if source == "upload_file":
            # ``UploadFile`` extraction: parse the multipart body on
            # first touch and cache the resulting file list on the
            # request so sibling parameters re-use the same parse.
            return await _extract_upload_file(
                request,
                extraction,
                fd,
                inner,
            )

        if source == "state":
            val = request.state.get(extraction.name)
            if val is None and request.app_state.has(extraction.name):
                val = request.app_state.get(extraction.name)
            if val is None:
                if extraction.has_default:
                    return extraction.default
                raise ExtractorFieldError(
                    f"missing state {extraction.name!r}",
                    detail={"field": extraction.name},
                )
            return val

        if source == "depends":
            if container is None:
                raise MissingProviderError(
                    "No DI container available for Depends extractor"
                )
            return await container.resolve(
                inner,
                request_cache=request_cache,
                framework_values={Request: request, type(request): request},
                owning_module=owning_module,
            )
    except ExtractorError:
        raise
    except Exception as exc:
        raise ExtractorError(
            f"failed to extract {extraction.name!r}: {exc}",
            detail={"field": extraction.name, "source": source},
        ) from exc

    # Custom extractor: dispatch to the marker's extract() method.
    #
    # Two dispatch paths:
    #   • @injectable + instance method → resolve instance from DI container
    #     so constructor-injected deps are available; call instance.extract(req, extraction).
    #   • classmethod (legacy) → call marker_cls.extract(req, extraction,
    #     container=..., request_cache=...) as before.
    marker_cls = extraction.marker_cls
    if marker_cls is not None and hasattr(marker_cls, "extract"):
        try:
            # Walk the MRO to find where 'extract' is actually defined so we
            # correctly detect classmethod vs. instance method regardless of
            # inheritance depth.
            _extract_attr: Any = None
            for _mro_cls in marker_cls.__mro__:
                if "extract" in _mro_cls.__dict__:
                    _extract_attr = _mro_cls.__dict__["extract"]
                    break
            _is_instance_method = not isinstance(
                _extract_attr, (classmethod, staticmethod)
            )
            # Check own __dict__ only — the DI container enforces the same
            # no-inheritance rule via MetadataInheritanceError, so a class
            # that merely inherits __lauren_injectable__ from a parent cannot
            # actually be resolved by the container.
            _is_injectable = "__lauren_injectable__" in marker_cls.__dict__

            if _is_injectable and _is_instance_method:
                # Injectable extractor: the DI container injects constructor
                # deps so extract() only needs (self, request, extraction).
                if container is None:
                    raise MissingProviderError(
                        f"Extractor {marker_cls.__name__!r} is @injectable but "
                        "no DI container is available; ensure it is registered in "
                        "a module's providers list.",
                    )
                instance = await container.resolve(
                    marker_cls,
                    request_cache=request_cache,
                    framework_values={Request: request, type(request): request},
                    owning_module=owning_module,
                )
                try:
                    _sig = _inspect.signature(instance.extract)
                    _params = _sig.parameters
                except (TypeError, ValueError):
                    _params = {}
                if "owning_module" in _params:
                    return await instance.extract(
                        request, extraction, owning_module=owning_module
                    )
                return await instance.extract(request, extraction)

            # Classic classmethod form — forward owning_module when declared.
            try:
                sig = _inspect.signature(marker_cls.extract)  # type: ignore[attr-defined]
                params = sig.parameters
            except (TypeError, ValueError):
                params = {}
            if "owning_module" in params:
                return await marker_cls.extract(  # type: ignore[attr-defined]
                    request,
                    extraction,
                    container=container,
                    request_cache=request_cache,
                    owning_module=owning_module,
                )
            return await marker_cls.extract(  # type: ignore[attr-defined]
                request,
                extraction,
                container=container,
                request_cache=request_cache,
            )
        except ExtractorError:
            raise
        except Exception as exc:
            # Let framework-level errors propagate unchanged:
            # • HTTPError subclasses (UnauthorizedError, ForbiddenError, …)
            #   map to their own HTTP status codes.
            # • StartupError / MissingProviderError are configuration errors
            #   that must surface directly, not be buried inside ExtractorError.
            from .exceptions import HTTPError, StartupError

            if isinstance(exc, (HTTPError, StartupError)):
                raise
            raise ExtractorError(
                f"custom extractor {marker_cls.__name__} failed: {exc}",
                detail={
                    "field": extraction.name,
                    "source": source,
                    "marker": marker_cls.__name__,
                },
            ) from exc

    raise ExtractorError(
        f"unknown extractor source {source!r}",
        detail={"source": source},
    )


# ---------------------------------------------------------------------------
# UploadFile extraction helpers
# ---------------------------------------------------------------------------


#: Attribute used to cache a parsed multipart body on a ``Request``
#: so sibling :class:`UploadFile` parameters reuse the same parse.
#: The name deliberately matches the framework's dunder convention
#: so collisions with user attributes are impossible.
_UPLOAD_CACHE_ATTR = "__lauren_upload_cache__"


async def _parse_multipart_once(request: Request) -> dict[str, list[Any]]:
    """Parse the multipart body of ``request`` at most once.

    Caches the resulting ``{field_name: [UploadFile, ...]}`` dict on
    the request object under :data:`_UPLOAD_CACHE_ATTR` so a handler
    with several ``UploadFile`` parameters triggers exactly one
    parse. The cache lives on the request only, so pooled
    :class:`Request` instances automatically start fresh on the
    next lease (``Request.reset`` wipes per-request attrs).
    """
    from ._multipart import iter_parts, parse_boundary
    from .types import UploadFile as _UploadFile

    cached = getattr(request, _UPLOAD_CACHE_ATTR, None)
    if cached is not None:
        return cached

    content_type = request.headers.get("content-type") or ""
    boundary = parse_boundary(content_type)
    body = await request.body()
    grouped: dict[str, list[Any]] = {}
    for part in iter_parts(body, boundary):
        upload = _UploadFile(
            data=part.data,
            filename=part.filename,
            content_type=part.content_type,
            headers=part.headers,
            name=part.name,
        )
        grouped.setdefault(part.name, []).append(upload)
    setattr(request, _UPLOAD_CACHE_ATTR, grouped)
    return grouped


async def _extract_upload_file(
    request: Request,
    extraction: Extraction,
    fd: "FieldDescriptor | None",
    inner: Any,
) -> Any:
    """Produce the value for an ``UploadFile`` / ``list[UploadFile]`` parameter.

    Matches parts against the handler parameter name (or the
    descriptor's ``alias`` override). Raises a clean
    :class:`ExtractorFieldError` when a required upload is absent,
    so the framework returns a 422 with a machine-readable payload
    rather than a generic 500.
    """
    grouped = await _parse_multipart_once(request)
    field_name = fd.alias if fd and fd.alias else extraction.name
    files = grouped.get(field_name, [])

    # Detect the shape the handler declared. ``UploadFile`` alone
    # returns a single instance; ``list[UploadFile]`` returns the
    # full list. The parser's ``inner`` is ``UploadFile`` for the
    # scalar case and ``list[UploadFile]`` for the collection case.

    origin = get_origin(inner)
    if origin in (list, tuple):
        if (
            not files
            and not extraction.has_default
            and not (fd and fd.default is not ...)
        ):
            raise ExtractorFieldError(
                f"missing upload {field_name!r}",
                detail={"field": field_name},
            )
        return list(files)

    # Scalar case: return the first matching part.
    if not files:
        if extraction.has_default:
            return extraction.default
        if fd and fd.default is not ...:
            return fd.default
        raise ExtractorFieldError(
            f"missing upload {field_name!r}",
            detail={"field": field_name},
        )
    return files[0]


def _validate_json(data: Any, target: Any, field_name: str) -> Any:
    # Discriminated-union validation — delegated to a Pydantic ``TypeAdapter``
    # so error messages point at the offending variant (feature 6).
    from .streaming import is_discriminated_union, _build_adapter

    if _PYDANTIC_AVAILABLE and is_discriminated_union(target):
        adapter = _build_adapter(target)
        try:
            return adapter.validate_python(data)
        except pydantic.ValidationError as e:
            raise ExtractorError(
                "validation error",
                detail={"field": field_name, "errors": e.errors()},
            ) from e
    if (
        _PYDANTIC_AVAILABLE
        and isinstance(target, type)
        and issubclass(target, _BaseModel)
    ):
        return _validate_pydantic(data, target, field_name)
    if target is Any or target is None:
        return data
    # primitive types
    if isinstance(data, target) if isinstance(target, type) else False:
        return data
    origin = get_origin(target)
    if origin in (list, tuple) and isinstance(data, list):
        return data
    if origin is dict and isinstance(data, dict):
        return data
    return data


def _validate_pydantic(data: Any, model: type, field_name: str) -> Any:
    try:
        return model.model_validate(data)  # type: ignore[attr-defined]
    except pydantic.ValidationError as e:
        raise ExtractorError(
            "validation error",
            detail={"field": field_name, "errors": e.errors()},
        ) from e


__all__ = [
    "ExtractionMarker",
    "Path",
    "Query",
    "Header",
    "Cookie",
    "Json",
    "Form",
    "Bytes",
    "ByteStream",
    "UploadFile",
    "State",
    "Depends",
    "FieldDescriptor",
    "PathField",
    "QueryField",
    "HeaderField",
    "CookieField",
    "pipe",
    "Pipe",
    "PipeContext",
    "PipeMeta",
    "PIPE_META",
    "is_pipe",
    "parse_extractor_hint",
    "extract_parameter",
    "Extraction",
]
