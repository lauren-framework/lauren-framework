"""Native validation engine (private; public surface: :mod:`lauren.validation`).

* ``_issues`` — :class:`ValidationIssue`, :class:`ValidationFailed`, the message catalogue;
* ``_markers`` — constraint markers, ``@validation_config``, :class:`ValidationPolicy`;
* ``_scalars`` — boolean tokens and ISO 8601 durations (shared with the config loader);
* ``_nodes`` — validator nodes (validate / project / schema);
* ``_backends`` — pydantic and msgspec nodes plus error translation;
* ``_compiler`` — :class:`TypeCompiler` and :class:`CompiledType`;
* ``_jsonschema`` — the JSON Schema 2020-12 / OpenAPI 3.1 emitter;
* ``_params`` — request-parameter, WebSocket-frame and stream-item plans built at startup.

This ``__init__`` imports nothing so the leaf modules stay cheap to import.
"""

from __future__ import annotations
