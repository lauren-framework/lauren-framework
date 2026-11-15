"""Body codecs and content negotiation.

The JSON codec is always registered; add others with
``LaurenFactory.create(..., content=ContentConfig(codecs=[MsgpackCodec()]))``.
Routes accept them with ``consumes=[...]`` and serve them with
``produces=[...]`` (negotiated from ``Accept``)::

    from lauren import ContentConfig, LaurenFactory, MsgpackCodec, get

    @get("/users/{id}", produces=["application/json", "application/msgpack"])
    async def show(self, id: int) -> UserOut: ...

    app = LaurenFactory.create(AppModule, content=ContentConfig(codecs=[MsgpackCodec()]))

A codec is any object with ``media_types`` (a tuple of concrete media types,
the first being its canonical response type), ``decode(bytes) -> Any`` and
``encode(value) -> bytes``.
"""

from __future__ import annotations

from ._codecs._codec import Codec, JsonCodec, MsgpackCodec
from .types import ContentConfig

__all__ = ["Codec", "ContentConfig", "JsonCodec", "MsgpackCodec"]
