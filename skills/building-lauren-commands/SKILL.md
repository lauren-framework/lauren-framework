---
name: building-lauren-commands
description: Runs Lauren application code without HTTP. Covers LaurenFactory.create_context / app.context() / ApplicationContext.from_lauren (startup and shutdown around scripts, hosted services off by default), ctx.get / ctx.resolve / ctx.scope, operation scopes for request-scoped providers, @command methods with arguments derived from the signature (positional, --kebab options, --flag/--no-flag, repeatable lists, Enum/Literal choices, Opt(short, env, help)), CommandContext (echo, confirm, fail), exit codes, python -m lauren exec / commands / console, testing with ctx.run_command and Test.create_testing_module, workers with wait_for_shutdown_signal, Kubernetes CronJobs, and strict_request_scope. Use instead of admin HTTP endpoints, ad-hoc asyncio.run scripts that resolve services from app.container, or calling private _open() hooks by hand.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Lauren Commands & Standalone Contexts

The API lives in `lauren.context` (`ApplicationContext`) and `lauren.commands`
(`command`, `Opt`, `CommandContext`, `CommandResult`, `CommandInfo`); the main
names are also exported from `lauren`. Guide: `docs/guides/commands-and-scripts.md`.

## Pick the tool

| Need | Use |
|---|---|
| Seed, migrate, purge, fix up data (operator-run) | `@command("db:seed")` + `python -m lauren exec` |
| A Python script that needs services | `async with LaurenFactory.create_context(AppModule) as ctx:` |
| Request-scoped providers (DB session, unit of work) in a script | `async with ctx.scope("name") as scope: await scope.resolve(DbSession)` |
| A queue worker / long-running process without HTTP | `create_context(WorkerModule, hosted_services=True)` + `await ctx.wait_for_shutdown_signal()` |
| Periodic heavy work in Kubernetes | a `CronJob` running `python -m lauren exec ...` |
| Periodic light work in the web process | `@cron` / `@interval` (see `building-lauren-scheduling`) |
| Poke at a running graph | `python -m lauren console app.main:AppModule` |

Never expose admin tasks as HTTP endpoints (`POST /api/seed` without a guard
lets anyone re-seed the database).

## Script with a context

```python
import asyncio

from lauren import LaurenFactory


async def main() -> None:
    async with LaurenFactory.create_context(AppModule) as ctx:  # @post_construct ran
        users = ctx.get(UserService)  # constructed singleton (sync)
        async with ctx.scope("backfill") as scope:
            session = await scope.resolve(DbSession)  # Scope.REQUEST: one per scope
            await users.backfill(session)
        # session torn down here: @pre_destruct, then aclose()
    # on_shutdown, @pre_destruct ran


asyncio.run(main())
```

- `create_context(root, *, hosted_services=False, scheduler=False, drain_timeout=10.0, **create_kwargs)` runs every factory phase (routes too) — `create_kwargs` go to `LaurenFactory.create`.
- `app.context()` wraps a built, **unstarted** `LaurenApp`; `ApplicationContext.from_lauren(lauren_app)` compiles a `Lauren()` app first. Wrapping a started app → `LifecycleViolationError` (`reason="app_already_started"`).
- `ctx.get(token)` → constructed singleton; request-scoped/transient → `DIScopeViolationError` (`reason="not_singleton"`).
- `await ctx.resolve(token)` → singleton or new transient; request-scoped → `DIScopeViolationError` (`reason="no_active_scope"`).
- Members other than `commands()` / `app` / `container` need the `async with` block (`LifecycleViolationError`, `reason="context_not_active"`).
- A context is single-use. A startup failure shuts down what had started, then propagates.

## Command

```python
import enum
from pathlib import Path
from typing import Annotated

from lauren import injectable
from lauren.commands import CommandContext, Opt, command


class Fixture(enum.Enum):
    DEMO = "demo"
    LOAD = "load-test"


@injectable()
class SeedCommands:
    def __init__(self, db: Database) -> None:  # constructor DI as usual
        self._db = db

    @command("db:seed", help="Insert fixture data.", aliases=("seed",))
    async def seed(
        self,
        ctx: CommandContext,  # injected
        uow: UnitOfWork,  # DI: resolved in the command's own scope
        fixture: Fixture = Fixture.DEMO,  # optional positional, choices
        *,
        count: Annotated[int, Opt(short="-n", env="SEED_COUNT", help="rows per table")] = 50,  # -n/--count
        reset: bool = False,  # --reset/--no-reset
    ) -> int:
        if reset and not ctx.confirm("Truncate every table?"):  # refuses when not interactive
            return 1
        ctx.echo(f"seeded {await self._db.seed(fixture, count)} rows")  # not print()
        return 0

    @command("db:export")
    async def export(self, target: Path, *, tables: list[str] | None = None) -> dict:
        ...  # returned dict is printed as JSON
```

List the provider in a module's `providers`. Mapping rules:

| Parameter | CLI |
|---|---|
| `CommandContext` | injected |
| visible provider type / `Annotated[T, Inject(token)]` | injected (provider's own module visibility) |
| `bool` | `--x / --no-x` |
| keyword-only | `--kebab-case` option (required without default) |
| other, no default / with default | required / optional positional |
| `list[T]`, `tuple[T, ...]` | repeatable option / variadic positional |
| `T \| None` | optional, default `None` |
| `Enum`, `Literal` | choices |

CLI types: `str int float Decimal bool Path UUID datetime date time timedelta`,
enums, literals. `Opt(short=..., env=...)` only on options.

## Exit codes

| Outcome | Code |
|---|---|
| return `None` / `True` / a JSON-able value (printed) | 0 |
| return `int` n | n |
| return `False` | 1 |
| `ctx.fail("msg", exit_code=2)` | 2 (msg on stderr) |
| uncaught exception | 1 (`--verbose` for the traceback) |
| unknown command / bad arguments | 2 |
| app cannot be imported, built or started (runner) | 3 |
| SIGINT / SIGTERM (runner) | 130 / 143 after teardown |

## Runner

```console
python -m lauren exec    [-t TARGET] [-y] [-v] [TARGET] COMMAND [ARGS...]
python -m lauren commands [-t TARGET] [--all] [TARGET]
python -m lauren console  [-t TARGET] [TARGET]
```

TARGET = `pkg.module:attr` (or `$LAUREN_TARGET`): `@module` class, `LaurenApp`,
`Lauren()` app, `ApplicationContext`, or a zero-arg (async) factory. `exec`
validates arguments before starting the app; `--yes` / `--verbose` work after
COMMAND too. `lauren-cli` (`lauren exec` …) wraps the same runner.

## Testing

```python
from lauren.testing import Test


async def test_seed() -> None:
    app = Test.create_testing_module(AppModule).override_provider(Database).use_value(FakeDb()).compile()
    async with app.context() as ctx:
        result = await ctx.run_command("db:seed", "--count", "3", "--yes")
    assert result.exit_code == 0
    assert "seeded" in result.stdout
```

`CommandResult(exit_code, value, stdout, stderr, exception)`. Prompts:
`assume_yes=True`, or `interactive=True, stdin=io.StringIO("y\n")`. Build test
apps with `strict_request_scope=True` so any request-scoped provider resolved
outside a request/scope raises `DIScopeViolationError` (`reason="no_active_scope"`)
instead of leaking (the default only warns once per provider).

## Startup errors

| Error | Cause | Fix |
|---|---|---|
| `DecoratorUsageError` | bare `@command`, invalid name, `Opt(short/env)` on a positional, conflicting option strings | `@command("db:seed")`; make the parameter keyword-only |
| `DuplicateBindingError` (`duplicate_command_name`) | two commands share a name/alias | rename |
| `UnresolvableParameterError` (`unsupported_command_parameter`) | a parameter is neither a CLI type nor a provider visible from the command's module | export/import the provider or use a CLI type |

## Pitfalls

- `print()` in a command is not captured by `ctx.run_command` — use `ctx.echo`.
- `await app.container.resolve(RequestScoped)` in a script leaks an instance — use `ctx.scope()`.
- Don't `await app.startup()` on an app you wrap in a context; the context owns the lifecycle.
- Hosted services and the scheduler are off in contexts unless enabled.
- Keep commands thin adapters over services.
