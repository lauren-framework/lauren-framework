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
* **Fast to code**: NestJS-style decorators (`@controller`, `@injectable`,
  `@module`) and Axum-style extractors (`Path[int]`, `Json[Model]`,
  `State[T]`) keep boilerplate to a minimum.
* **Fewer bugs**: Strict metadata inheritance, cycle detection, and
  Pydantic-validated extractors reduce a class of errors by design.
* **Intuitive**: Great editor support. Completion everywhere. Less time
  debugging.
* **Easy**: Designed to be easy to use and learn. Less time reading docs.
* **Short**: Minimize code duplication. Each parameter declaration carries
  its own validation.
* **Robust**: Get production-ready code. With automatic interactive
  documentation. 7-phase startup, lifespan, graceful drain.
* **Standards-based**: Built on top of the [ASGI](https://asgi.readthedocs.io/)
  spec, [Pydantic](https://docs.pydantic.dev/), and full [OpenAPI 3.1](https://www.openapis.org/)
  generation.

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

lauren stands on the shoulders of giants:

* [Starlette](https://www.starlette.dev/) for the ASGI plumbing under the
  hood (re-exported via `lauren.types`; you don't import Starlette
  directly).
* [Pydantic](https://docs.pydantic.dev/) for request validation and
  response serialisation.

Python **3.11**, **3.12**, and **3.13** are supported.

## Installation

Create and activate a [virtual environment](https://docs.python.org/3/library/venv.html)
and then install lauren:

```console
$ pip install lauren

---> 100%
```

You'll also want an ASGI server such as
[Uvicorn](https://www.uvicorn.org/) or [Hypercorn](https://hypercorn.readthedocs.io/):

```console
$ pip install "lauren[standard]"
```

The `standard` extra installs Uvicorn, the recommended JSON extractor
deps, and the docs CLI.

## Example

### Create it

Create a file `main.py` with:

```python
import asyncio
from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    controller,
    get,
    post,
    module,
    Path,
    Json,
    use_guards,
    ExecutionContext,
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
    async def get_user(self, id: Path[int]) -> dict:
        # Return a dict — auto-serialised to JSON.
        return {"id": id, "found": True}

    @post("/")
    async def create(self, body: Json[CreateUser]):
        # Tuple form: body + status code.
        return body.model_dump(), 201

    @get("/admin")
    @use_guards(AdminGuard)
    async def admin_only(self) -> dict:
        return {"access": "granted"}


@module(controllers=[UserController])
class AppModule:
    pass


async def main() -> None:
    app = LaurenFactory.create(AppModule)
    # `app` is a fully-typed ASGI 3 callable — serve with uvicorn:
    #   uvicorn main:app --reload


asyncio.run(main())
```

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
* The path `/users/{id}` has a *path parameter* `id` that is validated as an `int`.
* The path `/users/` accepts `POST` with a JSON body validated against
  the `CreateUser` Pydantic model.
* The path `/users/admin` is gated by a class-based guard and returns
  401 unless the `x-role: admin` header is present.

### Interactive API docs

Now go to <a href="http://127.0.0.1:8000/docs" target="_blank">http://127.0.0.1:8000/docs</a>.

You will see the automatic interactive API documentation (provided by
[Swagger UI](https://github.com/swagger-api/swagger-ui)):

![Swagger UI](https://lauren.dev/img/index/index-01-swagger-ui-simple.png)

### Alternative API docs

And now, go to <a href="http://127.0.0.1:8000/redoc" target="_blank">http://127.0.0.1:8000/redoc</a>.

You will see the alternative automatic documentation (provided by
[ReDoc](https://github.com/Rebilly/ReDoc)):

![ReDoc](https://lauren.dev/img/index/index-02-redoc-simple.png)

## Example upgrade

Now modify the file `main.py` to wire up dependency injection, a
lifecycle hook, and an `update` route on `UserController`.

```python
from lauren import injectable, post_construct, pre_destruct


@injectable()
class UserRepository:
    @post_construct
    async def warm(self) -> None:
        self.cache: dict[int, str] = {}

    @pre_destruct(timeout=5.0)
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
    async def get_user(self, id: Path[int]) -> dict:
        name = await self.repo.find(id)
        if name is None:
            return {"id": id, "found": False}, 404
        return {"id": id, "name": name}

    @post("/")
    async def create(self, body: Json[CreateUser]):
        await self.repo.upsert(body.age, body.name)
        return body.model_dump(), 201


@module(providers=[UserRepository], controllers=[UserController])
class AppModule:
    pass
```

### Interactive API docs upgrade

Now reload your browser at <a href="http://127.0.0.1:8000/docs" target="_blank">http://127.0.0.1:8000/docs</a>.

The interactive API docs will be automatically updated, including
the new request body for the `POST /users/` route &mdash; lauren generated
the JSON Schema directly from your Pydantic model.

### Recap

In summary, you declare **once** the types of parameters, body, and
guards as decorators or `Annotated` markers. lauren takes care of:

* Reading the request payload, validating it, and converting it to the
  right Python type.
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

lauren depends on Pydantic and Starlette.

### `standard` Dependencies

When you install lauren with `pip install "lauren[standard]"`, it comes
with the following extras:

* [`uvicorn`](https://www.uvicorn.org/) &mdash; the recommended ASGI server.
* [`uvicorn[standard]`](https://www.uvicorn.org/) &mdash; with
  `uvloop`, `httptools`, and `websockets`.
* [`orjson`](https://github.com/ijl/orjson) &mdash; faster JSON
  serialisation.
* [`python-multipart`](https://github.com/Kludex/python-multipart) &mdash;
  required for `Form[...]` and `File[...]` extractors.

### Without `standard` dependencies

If you don't want the optional `standard` dependencies, you can install
with `pip install lauren` instead.

### Additional optional dependencies

There are some additional dependencies you might want to install:

* [`httpx`](https://www.python-httpx.org/) &mdash; required for the test
  client (`lauren.testing`).
* [`pytest`](https://docs.pytest.org/) and
  [`pytest-asyncio`](https://pytest-asyncio.readthedocs.io/) &mdash; if
  you want to use the test client in your test suite.
* [`msgspec`](https://jcristharif.com/msgspec/) &mdash; if you prefer
  msgspec over Pydantic for hot-path serialisation, install it and use
  `MsgspecEncoder` from `lauren.serialization`.

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
