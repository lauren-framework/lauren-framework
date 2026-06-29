# What's Next

> You built Hero HQ from an empty folder to a deployed, tested, real-time API. The cape is
> earned. Here's where to go when you want to take a feature deeper than the tutorial did.

## You now know how to…

- [x] Route requests with `@controller` and the verb decorators
- [x] Validate input with `Json[...]` / `Path[...]` and return clean error envelopes
- [x] Wire services with dependency injection and the three scopes
- [x] Split an app into modules with explicit `imports` / `exports`
- [x] Guard routes and raise custom, codified errors
- [x] Keep per-client state with signed-cookie sessions
- [x] Stream with SSE, defer work with background tasks, and go real-time with WebSockets
- [x] Test the whole thing in-process, and ship it with clean lifecycle and OpenAPI docs

That's the spine of nearly every Lauren app. The rest is depth.

---

## Go deeper

<div class="grid cards" markdown>

-   :material-cube-outline: [__Core Concepts__](../core-concepts/index.md)

    ---
    The mental model behind what you just built: modules, controllers, injectables, the strict
    inheritance rules, lifecycle, and the request/response objects.

-   :material-tools: [__Guides__](../guides/index.md)

    ---
    ~30 task-focused recipes — custom extractors, pipes, interceptors, typed streaming,
    `propagate_metadata`, OpenAPI security from guards, and more.

-   :material-book-open-variant: [__Reference__](../reference/index.md)

    ---
    The full API surface, generated from docstrings — every decorator, extractor, type, and
    error code.

-   :material-package-variant: [__Companion packages__](https://github.com/lauren-framework/lauren-framework#companion-packages)

    ---
    `lauren-middlewares` (CORS, rate limit, …), `lauren-logging`, and `lauren-guards`
    (JWT, API key, OAuth2, CSRF) — production cross-cutting concerns, next door.

</div>

---

## Take Hero HQ further

The tutorial kept things in-memory and single-worker so you could focus on the framework. To
turn it into a real service, try:

- **Swap the storage.** Put the `HeroRepository`, `MissionLog`, and session store behind Redis
  or Postgres — the interfaces don't change, only the implementations.
- **Make logout revocable everywhere.** The [Sessions guide](../guides/sessions.md#revocation)
  shows how a `revocation_store` turns "log out" into "log out on every device."
- **Add real auth.** Replace the toy badge with [`lauren-guards`](https://github.com/lauren-framework/lauren-framework#companion-packages)'
  `jwt_bearer` or `api_key`, and protect routes by role.
- **Type your streams.** Trade raw SSE for [`StreamingResponse[T]`](../guides/typed-streaming.md),
  which content-negotiates SSE / NDJSON / JSON Lines from the `Accept` header.
- **Observe it.** Add structured logging with `lauren-logging` and request-id middleware from
  `lauren-middlewares`.

---

## The finished app

The complete, tested source for everything you built lives in the repository at
[`docs/tutorial/hero_hq/`](https://github.com/lauren-framework/lauren-framework/tree/main/docs/tutorial/hero_hq).
Clone it, run it, break it, fix it — that's the best next step of all.

Welcome to Hero HQ. Now go save a city. 🦸
