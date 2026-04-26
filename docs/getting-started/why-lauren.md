# Why Lauren?

> *Three frameworks walk into a bar. One is Rust, one is TypeScript, one is Python. Lauren is what the bartender writes down on a napkin afterwards.*

Lauren exists because the Python web ecosystem is full of excellent micro-frameworks that **don't scale to enterprise complexity**, and full of macro-frameworks that **don't feel like Python anymore**. Lauren is a deliberate attempt to find the spot in the middle — borrowing the best ideas from three projects we admire deeply.

## The three inspirations

### From [Axum](https://github.com/tokio-rs/axum) (Rust) — *the execution graph*

Axum's central insight is that everything you need to dispatch a request — the route, the extractors, the middleware stack — is **a value composed at startup**, not a thing reflected on at request time. Lauren copies that insight directly:

* Every route is compiled into a radix-tree node with a pre-resolved extraction plan.
* Middleware and guards are *attached to* the route node; they're not discovered by walking class hierarchies on each request.
* The dispatch path performs **zero reflection**. No `inspect`, no `get_type_hints`, no decorator interrogation. Just traversal.

The result: predictable performance, predictable behavior, and the ability to validate the entire app graph before accepting any traffic.

### From [NestJS](https://nestjs.com/) (TypeScript) — *modules, DI, and lifecycle*

NestJS proved that a full-fat IoC container with explicit module boundaries makes large codebases dramatically easier to navigate, test, and refactor. Lauren adopts:

* `@module(imports=[...], providers=[...], controllers=[...], exports=[...])` for explicit dependency boundaries.
* `@injectable(scope=Scope.SINGLETON | REQUEST | TRANSIENT)` for typed DI with Protocol binding and multi-bindings.
* `@post_construct` / `@pre_destruct` lifecycle hooks executed in topological order.
* Class-based controllers with constructor injection.
* Custom providers (`use_value`, `use_class`, `use_factory`, `use_existing`) for the cases where `@injectable` isn't enough.

These ideas already work brilliantly in TypeScript. Python's runtime introspection makes them even cleaner.

### From [FastAPI](https://fastapi.tiangolo.com/) (Python) — *Pydantic and OpenAPI*

FastAPI showed the Python community that **type hints are documentation**. Lauren takes the same baseline:

* Pydantic v2 models for body validation (`Json[Model]`), with descriptive 422 errors.
* OpenAPI 3.1 generated from controller decorators and method signatures, ready for Swagger UI / ReDoc.
* Field descriptors (`QueryField(ge=1, le=100)`, `HeaderField(alias=...)`) emit constraint metadata into the schema.

Where Lauren diverges: types are resolved into a **handler extraction plan** at startup, not at request time. The runtime cost of validation is the cost of validation — there's no decorator-walking overhead on top.

---

## What Lauren is *not*

Being honest about the trade-offs:

* **It's not a micro-framework.** If you want a single-file Flask-style app, use Flask or Starlette. Lauren expects modules, controllers, and explicit DI registration. The payoff is structural — but it costs five minutes of boilerplate at file zero.
* **It's not magic.** No globals, no implicit registration, no decorator scanning. Every dependency is *declared*. We deliberately rejected dynamic discovery patterns because they're the #1 cause of "works in dev, breaks in prod" failures.
* **It's not a sync framework.** ASGI-only. Sync handlers are accepted (the dispatcher adapts), but the runtime is async-first.

## The principles, in plain English

1. **Startup validates; runtime dispatches.** If a graph error can be detected, it's detected before the app accepts traffic. Cycles, missing providers, scope violations, route conflicts, ambiguous Protocols — all caught in `LaurenFactory.create(...)`.
2. **Decorators attach metadata; they never rewrite functions.** Every Lauren decorator sets a dunder attribute and returns the original object. No wrapping, no monkey-patching. Your handlers are still your handlers.
3. **Type hints are introspection-ready, but only at startup.** Lauren resolves type hints once, freezes the result, and uses it forever. Adding `from __future__ import annotations` to your code is fine — Lauren handles `ForwardRef` resolution centrally.
4. **No global state.** A `LaurenApp` owns a `DIContainer`. Multiple apps coexist in the same process — your tests rely on this.
5. **Async-first, but not async-only.** Sync providers and sync handlers work; the dispatch engine adapts.

## Who Lauren is for

Lauren is the right choice if you're:

* Building a service that has to **survive on-call rotations**, where misconfigurations need to fail loudly *and immediately*, not silently in production at 3 a.m.
* Working in a **multi-team codebase** where module boundaries and explicit exports matter more than a single-file demo.
* Migrating from **NestJS** and want the same mental model in Python.
* Coming from **Rust + Axum** and miss the "compile-the-router-once" approach.
* Already happy with **FastAPI**'s Pydantic ergonomics but want stricter DI, modules, and lifecycle.

Lauren is probably *not* the right choice if you're prototyping a 100-line script. Use Flask. (We do, sometimes.)

Ready? [Read the prominent features →](features.md)
