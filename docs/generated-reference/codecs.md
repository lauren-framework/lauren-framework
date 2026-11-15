# Body Codecs

Request codecs, response negotiation and content policy.

### `Codec`

Decode request bodies and encode responses for a set of media types.

``media_types`` lists the concrete media types the codec serves; the first
one is its canonical response type. ``decode`` raises ``ValueError``
(or a subclass, e.g. msgspec's ``DecodeError``) on malformed input —
the framework answers with the malformed-body error (422, or 400 with
``ContentConfig(malformed_body_status=400)``). Unexpected exceptions
propagate as handler failures (500). Implementations must be reentrant:
one instance serves every request.

### `JsonCodec`

The JSON codec (``application/json`` and every ``+json`` type).

``backend`` picks the decoder: ``"auto"`` (default) uses msgspec when it
is installed, ``"stdlib"`` always uses :mod:`json`, ``"msgspec"`` /
``"orjson"`` require that package. A body the fast parser rejects is
re-parsed with :mod:`json`, which has the final word — so the accepted
documents (``NaN``, huge integers, UTF-16 bodies) and the error messages
do not depend on what is installed. The one exception is opt-in:
orjson decodes integers beyond 64 bits as floats. With a fast backend,
pydantic bodies are also validated in one pass (``model_validate_json``).

``encode`` uses ``encoder`` (a :class:`~lauren.serialization.JSONEncoder`)
or the process-wide encoder. Inside an app, JSON responses are always
written by the app's ``json_encoder`` / ``@use_encoder``.

### `MsgpackCodec`

MessagePack (``application/msgpack``), backed by msgspec.

Requires the ``msgspec`` package. ``msgspec.Struct`` bodies decode and
validate in one ``msgspec.msgpack.decode``; other targets decode first and
validate like JSON bodies. Values msgspec cannot encode natively fall back
to the same rules as JSON responses (pydantic models, dataclasses, …).

### `ContentConfig`

```python
class ContentConfig(strict_content_type: bool | Literal['warn'] | None = None, malformed_body_status: int = 422, codecs: Sequence[Any] = (), default_produces: Sequence[str] = ('application/json',), strict_accept: bool = False, multipart: MultipartConfig | None = None)
```

Application-wide request-body content policy.

Pass it to ``LaurenFactory.create(..., content=ContentConfig(...))`` (or
``Lauren(content=...)``). ``strict_content_type`` decides what a JSON body
parameter — ``Json[T]``, an implicit model body, ``Discriminated[...]`` —
does with the request's ``Content-Type`` before any body byte is read:

* ``True`` — only ``application/json`` and ``application/*+json`` (charset
  absent or UTF-8) are accepted; anything else, including a body announced
  by ``Content-Length`` > 0 or ``Transfer-Encoding`` without a
  ``Content-Type``, is rejected with 415
  :class:`~lauren.exceptions.UnsupportedMediaTypeError`. This stops
  cross-site "simple" requests (HTML forms, ``fetch`` in ``no-cors``
  mode) from reaching cookie-authenticated JSON routes.
* ``"warn"`` — the same check, but mismatches are accepted and logged
  once per route and media type (a bounded number per route), so the
  impact can be measured before switching to ``True``.
* ``False`` — no check.
* ``None`` (default) — ``"warn"`` when ``sessions=`` is configured,
  otherwise ``False``. Setting any explicit value also silences the
  startup warning session-enabled apps get.

The same policy applies to ``Form[...]`` bodies. Routes that declare
``consumes=[...]`` and ``UploadFile`` parameters (``multipart/form-data``)
are always checked.

* ``malformed_body_status`` — the status of a body that cannot be decoded
  (malformed JSON): ``422`` (default; ``detail.reason`` is
  ``"malformed_body"``) or ``400`` (:class:`~lauren.exceptions.BadRequestError`
  with ``line`` / ``col`` when known).
* ``codecs`` — extra :class:`~lauren.codecs.Codec` instances, e.g.
  ``[MsgpackCodec()]``. The JSON codec is always present; pass a
  ``JsonCodec(backend="stdlib")`` to configure it.
* ``default_produces`` — the response media types of routes without
  ``produces=``, in preference order (``("application/json",)``).
* ``strict_accept`` — answer 406 :class:`~lauren.exceptions.NotAcceptableError`
  when ``Accept`` excludes every ``produces`` entry, instead of serving
  the first one.
