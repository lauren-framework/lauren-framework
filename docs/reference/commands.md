# Contexts & Commands

Standalone application contexts (`lauren.context`) and DI-aware commands
(`lauren.commands`). See the [Commands & Scripts guide](../guides/commands-and-scripts.md)
for the full story.

## ApplicationContext

::: lauren.context.ApplicationContext

`ctx.scope()` returns an [`OperationScope`](di.md).

## Commands

::: lauren.commands.command

::: lauren.commands.Opt

::: lauren.commands.CommandContext

::: lauren.commands.CommandResult

## Introspection

::: lauren.commands.CommandInfo

::: lauren.commands.CommandParameter

::: lauren.commands.CommandMeta

## `python -m lauren`

| Action | Usage | Starts the app |
|---|---|---|
| `exec` | `python -m lauren exec [-t TARGET] [-y] [-v] [TARGET] COMMAND [ARGS...]` | yes (after the arguments parse) |
| `commands` | `python -m lauren commands [-t TARGET] [--all] [TARGET]` | no |
| `console` | `python -m lauren console [-t TARGET] [-v] [TARGET]` | yes |

`TARGET` is `pkg.module:attr` (or `$LAUREN_TARGET`): a `@module` class, a
`LaurenApp`, a `Lauren` app, an `ApplicationContext`, or a zero-argument (sync
or async) factory returning one of them.

| Exit code | Meaning |
|---|---|
| `0` | Success (`None` / `True` returned, or another value printed as JSON) |
| `n` | The command returned the `int` `n` |
| `1` | Uncaught exception, `False` returned, or an unencodable return value |
| `2` | Usage error: bad runner arguments, malformed TARGET, unknown command, invalid command arguments |
| `3` | The application could not be imported, built or started |
| `130` / `143` | Interrupted by SIGINT / SIGTERM (after teardown) |
