---
name: inspecting-lauren-apps
description: Inspect and validate a compiled Lauren app without lifecycle startup. Use describe_app/app.describe for the versioned manifest of routes, provider/module graphs, effective guards, WebSocket gateways, mounts and OpenAPI hash; python -m lauren check/routes/openapi for CI, route audits and contract snapshots; interpret graph-aware StartupError hints, source locations and PEP 678 notes. Prefer this over starting a TestClient just to read routes or grepping logs for Mapped messages.
---

# Inspecting Lauren Apps

`LaurenFactory.create(AppModule)` compiles and validates the graph but **does
not** start lifecycle hooks. Do not enter `TestClient` or call `app.startup()`
when a CI audit only needs the compiled declaration.

## Pick an inspection surface

| Task | Use |
|---|---|
| Check that an app compiles, no connections | `python -m lauren check pkg.app:AppModule` |
| Annotate a PR with startup failures | `python -m lauren check --format github pkg.app:AppModule` |
| List routes and effective guards | `python -m lauren routes --json pkg.app:AppModule` |
| Export the OpenAPI document | `python -m lauren openapi export --out openapi.json pkg.app:AppModule` |
| Inspect modules, providers, scopes and WS gateways | `describe_app(app)` → `AppManifest` |
| Read a **running** app's routes | `get_all_routes(app)` — empty before startup, deliberately |
| Read decorators on a class without building an app | `reflect_routes(Controller)` / `reflect_guards(Controller)` |

## Snapshot the graph

```python fragment
from lauren import LaurenFactory
from lauren.reflect import describe_app

app = LaurenFactory.create(AppModule)
manifest = describe_app(app)  # no post_construct / hosted services
for route in manifest.routes:
    print(route.method, route.path, route.guards, route.module)
assert manifest.manifest_version == 1
snapshot = manifest.to_json()  # sorted names and keys, diffable
```

The default includes metadata **keys only**; `include_metadata_values=True`
must be used deliberately, and a `Secret` wrapper is still redacted. Never
store a raw credential in metadata and assume the manifest will redact it.

## Startup failure diagnostics

Common `StartupError`s keep their class, message and existing `detail` keys.
Additional keys may be `hints: list[str]`, `source: {file, line, symbol}` and
`docs_url`. `help:` / `docs:` notes show up in a plain traceback. A missing
module export identifies the owner and suggests `exports=[Token]`; a misspelled
annotation suggests a registered token; an unregistered class points at
`providers=[Class]`. Treat a hint as advice, not as permission to weaken a
guard or bypass validation.

`python -m lauren check` exit codes: 0 compiled, 1 startup error, 2 bad target
or usage. Import-time side effects in your own module still run when the CLI
imports it; move connections into `@post_construct`.

Guide: `docs/guides/tooling.md`; API: `docs/reference/reflect.md`.
