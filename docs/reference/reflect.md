# Reflect

The `lauren.reflect` sub-package provides the context types and metadata readers
that power native `@use_guards` / `@use_interceptors` support on
`@ws_controller` classes.

All symbols are also re-exported from the top-level `lauren` namespace.

## Context types

### `WsConnectionContext`

::: lauren.WsConnectionContext

### `WsUpgradeRequest`

::: lauren.WsUpgradeRequest

## Metadata readers

These functions read cross-cutting metadata from a class's **own** `__dict__`
only — no inheritance, matching Lauren's own-class rule for guard and interceptor
metadata.

::: lauren.reflect_guards

::: lauren.reflect_interceptors

::: lauren.reflect_middlewares

::: lauren.reflect_all

## Composer helpers

Low-level helpers used internally by the WS runtime. Available to extension
packages (e.g. custom transports, MCP adapters) that need to run guard or
interceptor chains outside of Lauren's HTTP pipeline.

::: lauren.reflect.apply_guards

::: lauren.reflect.apply_interceptors
