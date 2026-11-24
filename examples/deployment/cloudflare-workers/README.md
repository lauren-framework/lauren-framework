# Cloudflare Workers (Python)

This example runs Lauren through Cloudflare's Python Workers ASGI adapter.
Unlike the container examples, Python Workers execute Python on Pyodide
(WebAssembly) inside the Workers runtime.

## Prerequisites

- A Cloudflare account.
- Node.js and npm.
- `uv` installed.

## Local development

From this directory:

```bash
uv run pywrangler dev
```

The local Worker is available at `http://localhost:8787`. In another
terminal:

```bash
curl http://localhost:8787/
```

## Deploy

Authenticate Wrangler, then deploy:

```bash
npx wrangler login
uv run pywrangler deploy
```

## How the adapter works

- `src/app.py` creates the ordinary Lauren ASGI application.
- `src/worker.py` exports `Default = asgi.entrypoint(app)`.
- `wrangler.jsonc` selects the Python Worker entry point and enables the
  `python_workers` compatibility flag.
- `workers-py` and `workers-runtime-sdk` provide the local/deployment
  tooling and runtime SDK.

## Runtime compatibility

Python Workers use Pyodide rather than a conventional CPython process. Before
using Lauren for production workloads, validate the exact framework version
and every dependency against the Workers runtime.

In particular, test:

- AnyIO backend and thread-pool behavior used by synchronous route handlers.
- Application startup/shutdown hooks and resources with runtime-specific APIs.
- Streaming/SSE and client disconnect behavior.
- Third-party packages that depend on native extensions, sockets, subprocesses,
  filesystem semantics, or unsupported CPython facilities.

This minimal example uses only a basic asynchronous route and does not claim
that every Lauren feature is supported on Workers.

See Cloudflare's
[Python Workers documentation](https://developers.cloudflare.com/workers/languages/python/)
and the
[ASGI framework integration announcement](https://developers.cloudflare.com/changelog/post/2026-09-02-python-workers-web-framework-support/).
