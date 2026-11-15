# HTTP Routing

Router policies, typed path converters and host parameters.

### `RoutingConfig`

How the router answers HTTP: implicit methods, path policies and converters.

Pass it as ``LaurenFactory.create(..., routing=RoutingConfig(...))`` (or
``Lauren(routing=...)``). Every value is validated when the object is
built; a bad one raises :class:`~lauren.exceptions.StartupError` with
``detail["reason"] == "invalid_routing_config"``.

* ``auto_head`` (default ``True``) — a path with a ``GET`` handler and no
  explicit ``@head`` answers ``HEAD`` by running the ``GET`` handler
  (``request.method`` stays ``"HEAD"``); the body is never sent. ``False``
  restores 405 for such requests.
* ``auto_options`` (default ``False``) — an ``OPTIONS`` request to a path
  that has routes but no ``@options`` handler gets ``204`` with ``Allow``.
  Global middlewares run first, so a CORS middleware still answers
  preflights.
* ``trailing_slash`` — ``"ignore"`` (default: ``/x`` and ``/x/`` are the
  same route), ``"strict"`` (distinct templates; the other spelling is a
  404) or ``"redirect"`` (the declared spelling is canonical; the other
  one gets a 308 to it, query string and root path kept).
* ``merge_slashes`` — collapse runs of ``/`` before matching (with
  ``"redirect"``: redirect to the merged path).
* ``match_raw_path`` — match on the raw request path (``scope["raw_path"]``)
  and percent-decode each segment, so ``/files/a%2Fb`` reaches ``{name}``
  as ``"a/b"``; malformed encoding is a 400.
* ``converters`` — custom :class:`~lauren.routing.PathConverter` instances
  by template name (``{"isbn": IsbnConverter()}``).
* ``infer_converters`` — an untyped ``{name}`` whose handler parameter is
  annotated ``int`` / ``uuid.UUID`` routes as ``{name:sint}`` /
  ``{name:uuid}``, so a malformed value is a 404 instead of a 422.

### `PathConverter`

Base class (and protocol) for typed path segments: ``{name:converter}``.

Subclass it and register an instance by name with
``RoutingConfig(converters={"isbn": IsbnConverter()})``; any object with
the same attributes works too. A converter decides whether a *whole*
segment belongs to the route (:meth:`matches` — a mismatch makes the
router try the next candidate, then answer 404), converts a matched
segment (:meth:`to_python`), formats a value back into a segment
(:meth:`to_url`, used by URL generation; it must produce a segment that
:meth:`matches`) and documents the parameter (:meth:`openapi_schema`).
``python_type`` becomes the parameter's type when the handler leaves it
unannotated. Custom converters take no arguments in templates and are
tried before every built-in one at the same position.

### `HostParam`

```python
class HostParam
```

A ``{name}`` label of the route's ``host=`` pattern: ``tenant: HostParam[str]``.

The value comes from the matched host (lower-cased, port dropped) and is
validated like a path parameter. The parameter name must be a label of
the controller's / module's ``host=`` pattern, else startup fails.
