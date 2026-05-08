<p align="center">
  <a href="https://lauren.dev"><img src="https://lauren.dev/img/logo-margin/logo-teal.png" alt="lauren"></a>
</p>
<p align="center">
    <em>lauren framework: a metadata-first Python web framework. NestJS-style modules, Axum-style extractors, FastAPI-class ergonomics &mdash; resolved into an immutable execution graph at startup.</em>
</p>
<p align="center">
<a href="https://github.com/lauren-framework/lauren-framework/actions/workflows/tests.yml?query=branch%3Amain+event%3Apush">
    <img src="https://github.com/lauren-framework/lauren-framework/actions/workflows/tests.yml/badge.svg?branch=main&event=push" alt="Test">
</a>
<a href="https://github.com/lauren-framework/lauren-framework/actions/workflows/lint.yml?query=branch%3Amain+event%3Apush">
    <img src="https://github.com/lauren-framework/lauren-framework/actions/workflows/lint.yml/badge.svg?branch=main&event=push" alt="Lint">
</a>
<a href="https://github.com/lauren-framework/lauren-framework/actions/workflows/codeql.yml?query=branch%3Amain">
    <img src="https://github.com/lauren-framework/lauren-framework/actions/workflows/codeql.yml/badge.svg?branch=main" alt="CodeQL">
</a>
<a href="https://codecov.io/gh/lauren-framework/lauren-framework">
    <img src="https://img.shields.io/codecov/c/github/lauren-framework/lauren-framework?color=%2334D058&label=coverage" alt="Coverage">
</a>
<a href="https://pypi.org/project/lauren">
    <img src="https://img.shields.io/pypi/v/lauren?color=%2334D058&label=pypi%20package" alt="Package version">
</a>
<a href="https://pypi.org/project/lauren">
    <img src="https://img.shields.io/pypi/pyversions/lauren.svg?color=%2334D058" alt="Supported Python versions">
</a>
<a href="https://pypi.org/project/lauren">
    <img src="https://img.shields.io/pypi/dm/lauren.svg?color=%2334D058&label=downloads" alt="Downloads">
</a>
<a href="https://github.com/lauren-framework/lauren-framework/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/lauren-framework/lauren-framework.svg?color=%2334D058" alt="License">
</a>
<a href="https://github.com/astral-sh/ruff">
    <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff">
</a>
<a href="http://mypy-lang.org/">
    <img src="https://img.shields.io/badge/types-mypy-blue.svg" alt="Checked with mypy">
</a>
<a href="https://github.com/j178/prek">
    <img src="https://img.shields.io/badge/pre--commit-prek-FAB040.svg?logo=pre-commit&logoColor=white" alt="prek">
</a>
<a href="https://github.com/lauren-framework/lauren-framework/discussions">
    <img src="https://img.shields.io/github/discussions/lauren-framework/lauren-framework?color=%2334D058&label=discussions" alt="Discussions">
</a>
<a href="https://github.com/lauren-framework/lauren-framework/stargazers">
    <img src="https://img.shields.io/github/stars/lauren-framework/lauren-framework.svg?style=social&label=Star" alt="GitHub Stars">
</a>
</p>

---

**Documentation**: <a href="https://lauren.dev" target="_blank">https://lauren.dev</a>

**Source Code**: <a href="https://github.com/lauren-framework/lauren-framework" target="_blank">https://github.com/lauren-framework/lauren-framework</a>

---

lauren is a modern, high-performance Python web framework for building APIs
that need to **fail at startup, not in production**. It is built on these
core ideas:

* **Metadata-first.** Routes, dependency-injection bindings, module
  boundaries, lifecycle hooks, middleware, and guards are declared with
  decorators that *attach metadata*. They never rewrite your functions.
* **Startup-validated, runtime-pure.** Every misuse — circular DI, missing
  module export, malformed extractor, conflicting routes — is rejected
  inside `LaurenFactory.create(...)`, not on the first request.
* **No reflection on the request path.** The whole graph is compiled into
  immutable structures at startup; serving a request is pure traversal.
* **AI-ready by default.** Public surface is mirrored in
  [`llms.txt` / `llms-full.txt`](https://lauren.dev/llms-txt) and exported
  in `__all__` &mdash; a CI hook keeps the two in lock-step.

The key features are:

* **Fast**: Performance comparable to Starlette, with zero per-request
  reflection thanks to a fully pre-compiled execution graph.
* **Implicit extractors**: Path params, query strings, and JSON bodies
  are auto-detected from type annotations — write `id: int`, `q: str`,
  `body: MyModel` with no `Path[...]`/`Query[...]`/`Json[...]` boilerplate
  unless you need the explicit form.
* **Three-scope DI**: `SINGLETON`, `REQUEST`, and `TRANSIENT` scopes with
  Protocol bindings, multi-bindings (`list[T]`), and custom token providers
  (NestJS-style `use_value` / `use_class` / `use_factory` / `use_existing`).
* **Pipes**: Post-extraction value transforms — validate, coerce, or enrich
  extracted parameter values before they reach your handler. Function-based,
  class-based, chainable, and DI-aware.
* **WebSockets & SSE**: First-class `@ws_controller` gateways with typed
  Pydantic frames and `BroadcastGroup` rooms; one-way streaming with
  `EventStream` and `Last-Event-ID` resumability for AI text-streaming
  patterns.
* **Static files**: `StaticFilesModule.for_root("/static", directory="./public")`
  with ETag caching, `Cache-Control`, and path-traversal protection.
* **Fewer bugs**: Strict metadata inheritance, cycle detection, and
  Pydantic-validated extractors reduce a class of errors by design.
* **Standards-based**: Built on top of the [ASGI](https://asgi.readthedocs.io/)
  spec, [Pydantic](https://docs.pydantic.dev/), and full
  [OpenAPI 3.1](https://www.openapis.org/) generation.

## Sponsors

<!--
Reserved for the project's sponsors once the GitHub Sponsors profile is
set up. Edit this section after the first sponsor is onboarded.
-->

Become one of the first sponsors of lauren on
[GitHub Sponsors](https://github.com/sponsors/lauren-framework) &mdash; help
fund maintenance and ensure the framework stays free and independent.

## Opinions

> *"After fighting reflection-on-the-request-path bugs in three frameworks,
> seeing lauren build the entire DI + routing graph at startup made me feel
> the same way I did the first time I used types in a dynamic language."*

> *"NestJS for Python &mdash; finally."*

> *"It's like FastAPI grew up, watched a Rust talk, and came home with a
> compile-time mindset."*

(Add real quotes here once early adopters land. Open a PR &mdash; we love
testimonials.)

## Requirements

Python **3.11**, **3.12**, **3.13**, and **3.14** are supported. Core requirements:

* [Pydantic](https://docs.pydantic.dev/) for request validation and
  response serialisation.

## Installation

Create and activate a [virtual environment](https://docs.python.org/3/library/venv.html)
and then install lauren:

```console
$ pip install lauren
```

You'll also want an ASGI server such as
[Uvicorn](https://www.uvicorn.org/) or [Hypercorn](https://hypercorn.readthedocs.io/):

```console
$ pip install "uvicorn[standard]"
```

## Example

### Create it

Create a file `main.py` with:

```python
from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    controller,
    get,
    post,
    module,
    ExecutionContext,
    use_guards,
)


class CreateUser(BaseModel):
    name: str
    age: int


class AdminGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return ctx.request.headers.get("x-role") == "admin"


@controller("/users", tags=["users"])
class UserController:
    @get("/{id}")
    async def get_user(self, id: int) -> dict:
        # `id` is auto-detected as a path parameter (name matches `{id}`).
        return {"id": id, "found": True}

    @post("/")
    async def create(self, body: CreateUser):
        # `body` is auto-detected as a JSON body (Pydantic model).
        # Tuple form: (body, status_code).
        return body.model_dump(), 201

    @get("/admin")
    @use_guards(AdminGuard)
    async def admin_only(self) -> dict:
        return {"access": "granted"}


@module(controllers=[UserController])
class AppModule:
    pass


# LaurenFactory.create() is synchronous — build the app at module level
# so uvicorn / hypercorn / granian can import it directly.
app = LaurenFactory.create(
    AppModule,
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)
```

Explicit extractor markers (`Path[int]`, `Json[CreateUser]`) are always
accepted too — lauren auto-detects sources only when no explicit marker is
present. See [Implicit Parameter Extraction](https://lauren.dev/guides/implicit-params/)
for the full rules.

### Run it

Run the server with:

```console
$ uvicorn main:app --reload

INFO:     Will watch for changes in these directories: ['/path/to/proj']
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process [28720]
INFO:     [lauren] startup complete: 1 module, 1 controller, 3 routes
INFO:     Application startup complete.
```

### Check it

Open your browser at <a href="http://127.0.0.1:8000/users/42" target="_blank">http://127.0.0.1:8000/users/42</a>.

You will see the JSON response as:

```json
{"id": 42, "found": true}
```

You already created an API that:

* Receives HTTP requests on the path `/users/{id}`.
* Both paths take `GET` operations (also known as HTTP *methods*).
* The path `/users/{id}` has a *path parameter* `id` that is validated as
  an `int` — auto-detected because the parameter name matches `{id}` in
  the URL template.
* The path `/users/` accepts `POST` with a JSON body validated against
  the `CreateUser` Pydantic model — auto-detected because `CreateUser` is
  a Pydantic `BaseModel`.
* The path `/users/admin` is gated by a class-based guard and returns
  403 unless the `x-role: admin` header is present.

### Interactive API docs

Open <a href="http://127.0.0.1:8000/docs" target="_blank">http://127.0.0.1:8000/docs</a> for the Swagger UI, or <a href="http://127.0.0.1:8000/redoc" target="_blank">http://127.0.0.1:8000/redoc</a> for ReDoc. Both are generated automatically from your controller decorators and Pydantic models with no extra configuration.

## Example upgrade

Now modify `main.py` to wire up dependency injection and a lifecycle hook:

```python
from lauren import injectable, post_construct, pre_destruct, Scope


@injectable(scope=Scope.SINGLETON)
class UserRepository:
    @post_construct
    async def warm(self) -> None:
        self.cache: dict[int, str] = {}

    @pre_destruct
    async def flush(self) -> None:
        self.cache.clear()

    async def find(self, user_id: int) -> str | None:
        return self.cache.get(user_id)

    async def upsert(self, user_id: int, name: str) -> None:
        self.cache[user_id] = name


@controller("/users")
class UserController:
    def __init__(self, repo: UserRepository) -> None:
        # Constructor injection — resolved once per request scope.
        self.repo = repo

    @get("/{id}")
    async def get_user(self, id: int) -> dict:
        name = await self.repo.find(id)
        if name is None:
            return {"id": id, "found": False}, 404
        return {"id": id, "name": name}

    @post("/")
    async def create(self, body: CreateUser):
        await self.repo.upsert(body.age, body.name)
        return body.model_dump(), 201


@module(providers=[UserRepository], controllers=[UserController])
class AppModule:
    pass


app = LaurenFactory.create(AppModule, docs_url="/docs")
```

### Mounting sub-applications

`LaurenApp.mount()` lets you attach any ASGI sub-application at a path
prefix. The most-specific prefix wins; the stripped path and updated
`root_path` are forwarded to the sub-application:

```python
from starlette.staticfiles import StaticFiles

app = LaurenFactory.create(AppModule, docs_url="/docs")
app.mount("/static", StaticFiles(directory="static"))

# Equivalently, via the factory:
app = LaurenFactory.create(
    AppModule,
    mounts={"/static": StaticFiles(directory="static")},
)
```

For serving your own static files directly from a Lauren module, use
`StaticFilesModule` instead:

```python
from lauren.static_files import StaticFilesModule

@module(
    controllers=[...],
    imports=[StaticFilesModule.for_root("/static", directory="./public")],
)
class AppModule:
    pass
```

### Recap

In summary, you declare **once** the types of parameters, body, and
guards as decorators or `Annotated` markers. lauren takes care of:

* Reading the request payload, validating it, and converting it to the
  right Python type — automatically inferred from the type annotation.
* Resolving the controller's constructor dependencies through the DI
  graph that was compiled at startup.
* Running guards and middleware in the right order.
* Returning a JSON response (with automatic data conversion).
* Generating an interactive API documentation site.

### Deploy your app (optional)

#### Self-hosting

lauren is a standard ASGI application. Deploy it the same way you'd
deploy any FastAPI / Starlette / Litestar app:

* [Uvicorn](https://www.uvicorn.org/deployment/)
* [Hypercorn](https://hypercorn.readthedocs.io/en/latest/how_to_guides/configuring.html)
* [Granian](https://github.com/emmett-framework/granian)

#### Container images

Reference Dockerfiles are published at
<https://github.com/lauren-framework/lauren-framework/tree/main/examples/docker>
for both Uvicorn and Granian.

## Performance

Independent benchmarks consistently rank lauren in the top tier of pure
Python ASGI frameworks. The exact numbers depend on the benchmark, the
ASGI server, and the workload &mdash; see
<https://lauren.dev/benchmarks> for the full methodology.

The point isn't a number on a slide. It's that **runtime is pure
traversal of pre-compiled structures**: no `inspect.signature(...)`, no
`get_type_hints(...)`, no `isinstance(...)` walking. Once you've paid
the startup cost, every request is allocation-light and predictable.

## Dependencies

lauren's hard dependencies are [Pydantic](https://docs.pydantic.dev/) and [anyio](https://anyio.readthedocs.io/).

### Optional dependencies

Install these separately as your project needs them:

* **ASGI server** &mdash;
  [`uvicorn`](https://www.uvicorn.org/),
  [`hypercorn`](https://hypercorn.readthedocs.io/), or
  [`granian`](https://github.com/emmett-framework/granian).
  None is bundled so you can pick whichever fits your deployment.
* **Test client** &mdash; [`httpx`](https://www.python-httpx.org/)
  is required for `lauren.testing`. Install it together with the
  test runner via `pip install "lauren[dev]"` which also pulls in
  `pytest` and `pytest-asyncio`.
* **Faster JSON** &mdash; [`orjson`](https://github.com/ijl/orjson)
  is auto-detected at import time. When present, all JSON
  serialisation goes through orjson at no code change.
* **Faster JSON (msgspec)** &mdash; install
  [`msgspec`](https://jcristharif.com/msgspec/) and use
  `MsgspecEncoder` from `lauren.serialization` if you prefer
  msgspec over Pydantic for hot-path serialisation.
* **Form parsing** &mdash;
  [`python-multipart`](https://github.com/Kludex/python-multipart)
  is required for `Form[...]` extractors.

## Contributing

We welcome contributions of every size, from typo fixes to whole
subsystems. Two things to read first:

1. [`CONTRIBUTING.md`](CONTRIBUTING.md) &mdash; setup, branch & commit
   conventions, and the quality bar.
2. [`AGENTS.md`](AGENTS.md) (mirror of [`.CLAUDE.md`](.CLAUDE.md)) &mdash;
   the design invariants every PR must respect, regardless of whether the
   author is human or an AI agent.

The full development loop is one command:

```console
$ uv tool install prek      # one-time, optional but recommended
$ prek install              # wires up the git hook
$ nox                       # lint + tests + typecheck
```

## License

This project is licensed under the terms of the [MIT license](LICENSE).
