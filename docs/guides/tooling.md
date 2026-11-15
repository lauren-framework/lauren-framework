# Inspecting a compiled app

Lauren validates its module, provider and route graphs when
`LaurenFactory.create(...)` runs. You can inspect that compiled graph without
starting lifecycle hooks, opening a database, or serving a request.

## Validate in CI

```bash
python -m lauren check myapp.modules:AppModule
python -m lauren check --format json myapp.modules:AppModule
python -m lauren check --format github myapp.modules:AppModule
```

`TARGET` can also be an existing app or a zero-argument factory (`module:attr`),
and defaults to `$LAUREN_TARGET`. Exit codes: **0** = compiled, **1** = startup
validation failed, **2** = target cannot be imported or the command was used
incorrectly. A successful check does **not** run `@post_construct` or
`@hosted_service`; keep module-level imports free of side effects.

The GitHub format emits `::error file=...,line=...::...` annotations. Common
startup errors include `detail["hints"]` with a specific fix, `detail["source"]`
with `file`, `line` and `symbol`, `detail["dependency_path"]` for cycle/scope
errors, and `detail["docs_url"]` pointing to the
[error catalog](../reference/errors.md). The same help and docs links appear
as PEP 678 notes in an ordinary traceback. Existing exception classes,
messages, codes and detail keys are unchanged.

## Routes and OpenAPI without startup

```bash
python -m lauren routes myapp.modules:AppModule
python -m lauren routes --json --method POST --path /admin myapp.modules:AppModule
python -m lauren openapi export --out openapi.json myapp.modules:AppModule
```

`routes` includes HTTP routes and WebSocket gateways; `--module` filters by
owning module and `--with-chain` shows middleware, guards and interceptors.
`openapi` can print JSON to standard output instead of writing a file. These
are intentionally plain-text, stdlib-only fallbacks; the companion
`lauren-cli` owns richer tables, dependency graphs and manifest comparisons.

## A versioned, deterministic manifest

<!-- docs-contract: run -->
```python
from lauren import LaurenFactory, controller, get, module
from lauren.reflect import describe_app

@controller("/users")
class Users:
    @get("/{id}")
    async def show(self, id: int) -> dict:
        return {"id": id}

@module(controllers=[Users])
class AppModule:
    pass

app = LaurenFactory.create(AppModule)
manifest = describe_app(app)  # no app.startup(), no lifecycle hooks
assert manifest.manifest_version == 1
assert any(route.path == "/users/{id}" for route in manifest.routes)
assert manifest.to_dict()["routes"]
assert app.describe().to_json() == manifest.to_json()
```

`describe_app(app)` works before **and** after startup. It is separate from
`get_all_routes(app)` and `get_all_ws_gateways(app)`, which intentionally return
nothing before startup. `AppManifest` is a frozen result tree with `app`,
`globals`, `modules`, `providers`, `routes`, `websocket_gateways`, `mounts`,
`openapi` (SHA-256 and operation count) and an extension `sections` mapping.
Modules and providers are sorted; routes are ordered by path then method.
`to_json(indent=2)` produces deterministic JSON suitable for review and
snapshot comparisons.

A route entry includes compiled parameters, the **effective** policy chains
(including global guards), owning module, binding, encoder, status, tags and
metadata **keys**. Metadata values are omitted by default because they may
contain credentials. `describe_app(app, include_metadata_values=True)` copies
JSON-safe values, but still redacts nested `Secret[...]` values. Avoid putting
raw credentials in unwrapped metadata or module names.

The manifest schema is versioned (`manifest_version=1`); consumers should
ignore additional fields within that version. Compare a saved manifest against
a changed app to detect removed routes, guards or exports; always review an
automated diff rather than treating a manifest as the sole security boundary.
