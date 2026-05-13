# Installation

Lauren targets **Python 3.11 or newer** and ships with `py.typed`, so static analyzers like mypy and pyright pick up the full public API immediately.

## Install from PyPI

```bash
pip install lauren
```

For Pydantic-validated request bodies (`Json[Model]`, `Form[Model]`, response models in OpenAPI), install Pydantic v2:

```bash
pip install pydantic
```

## Install from source

```bash
git clone https://github.com/lauren-framework/lauren-framework.git
cd lauren-framework
pip install -e .
pip install pytest pytest-asyncio   # to run the test suite
```

## Run the tests

Lauren ships with **1500+ unit and integration tests** covering routing, DI, extractors, modules, lifecycle, middleware, guards, exception handlers, OpenAPI, the strict inheritance guard, auto-serialization, structured logging, signal handling, WebSockets, Server-Sent Events, Socket.IO, streaming, and background tasks:

```bash
python -m pytest tests/
```

## Pick an ASGI server

Lauren returns a standard ASGI 3 callable from `LaurenFactory.create(...)`. Any compliant server works:

=== "uvicorn"

    ```bash
    pip install "uvicorn[standard]"
    uvicorn myapp:app --host 0.0.0.0 --port 8000
    ```

=== "hypercorn"

    ```bash
    pip install hypercorn
    hypercorn myapp:app --bind 0.0.0.0:8000
    ```

=== "granian"

    ```bash
    pip install granian
    granian --interface asgi myapp:app
    ```

## Recommended editor setup

| Tool | Why it helps |
|---|---|
| **mypy** or **pyright** | Lauren's `py.typed` marker means type checkers see every extractor, scope, and DI signature. Catches misuse statically. |
| **Ruff** | The framework's style is Black-compatible (88-char soft limit, 120 hard). Ruff's defaults align cleanly. |
| **uvicorn `--reload`** | Hot reload during development; Lauren's startup is fast enough that reloads feel instant on most graphs. |

You're ready. Head to the [Quickstart](quickstart.md) to build your first app.
