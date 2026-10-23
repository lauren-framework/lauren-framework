# Commands & Scripts

Every service has entry points that are not HTTP: seeding a database, running
a migration, a one-off fix-up, a Kubernetes `CronJob`, a queue worker. They
need the same DI graph, configuration and lifecycle as the server — the
database service whose `@post_construct` opens the pool, the settings, the
request-scoped unit of work that commits on teardown.

Lauren gives them three tools:

* **`ApplicationContext`** — `LaurenFactory.create_context(AppModule)` builds
  the exact graph the server builds and runs its lifecycle, without serving
  HTTP.
* **Operation scopes** — `ctx.scope()` (or `app.scope()`) is the request scope
  without a request: request-scoped providers live as long as the block and
  are torn down deterministically.
* **`@command`** — a method on any provider becomes a CLI command, discovered
  and validated at startup, run with `python -m lauren exec`.

Never expose admin tasks as HTTP endpoints. A `POST /api/seed` route without
a guard lets anyone who can reach the API re-seed the database; a command runs
only where the operator controls the process.

---

## Run code with the app's graph

<!-- docs-contract: run -->
```python
import asyncio

from lauren import LaurenFactory, Scope, injectable, module, post_construct, pre_destruct


@injectable()
class Database:
    def __init__(self) -> None:
        self.connected = False
        self.users = ["ada", "grace"]

    @post_construct
    async def connect(self) -> None:
        self.connected = True

    @pre_destruct
    async def disconnect(self) -> None:
        self.connected = False


@injectable(scope=Scope.REQUEST)
class DbSession:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.closed = False

    async def aclose(self) -> None:  # runs when the scope ends
        self.closed = True


@injectable()
class UserService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def backfill(self, session: DbSession) -> int:
        return len(session.db.users)


@module(providers=[Database, DbSession, UserService])
class AppModule:
    pass


async def main() -> None:
    async with LaurenFactory.create_context(AppModule) as ctx:  # startup: @post_construct
        users = ctx.get(UserService)  # an already constructed singleton
        async with ctx.scope("backfill") as scope:
            session = await scope.resolve(DbSession)  # one instance for the block
            assert await users.backfill(session) == 2
        assert session.closed  # torn down when the block exits
        db = ctx.get(Database)
        assert db.connected
    assert not db.connected  # shutdown: @pre_destruct


asyncio.run(main())
```

`create_context()` runs every phase of `LaurenFactory.create` — routes
included, so if a script boots, the server boots — and passes any other
keyword argument to it (`global_providers=`, `json_encoder=`,
`strict_request_scope=`, …). Entering the context runs `startup()`; leaving it
runs `shutdown()`, also when the block raises (the exception propagates
afterwards). A startup failure shuts down whatever had started before
propagating. Contexts are single-use.

| Member | What it does |
|---|---|
| `ctx.get(token)` | Returns an already constructed singleton, synchronously. Request-scoped and transient tokens raise `DIScopeViolationError` (`reason="not_singleton"`) |
| `await ctx.resolve(token)` | Resolves a singleton or a new transient. A request-scoped token raises `DIScopeViolationError` (`reason="no_active_scope"`): use a scope |
| `ctx.scope(name="context", *, values=None)` | Opens an `OperationScope` (see [Dependency Injection](dependency-injection.md#part-g-scopes-outside-requests-appscope)) |
| `await ctx.run_command(name, *argv)` | Runs a command and returns a `CommandResult` |
| `ctx.commands()` | Lists the app's commands (works before entering) |
| `await ctx.wait_for_shutdown_signal()` | Blocks until SIGINT / SIGTERM (workers) |
| `ctx.app`, `ctx.container` | Escape hatches |

Already have an app object? `app.context()` wraps a built `LaurenApp` that
has not been started, and `ApplicationContext.from_lauren(app)` compiles and
wraps an imperative `Lauren()` app. Wrapping an app that was already started
raises `LifecycleViolationError`: one lifecycle has one owner.

Hosted services and the scheduler are **off** in a context. Pass
`hosted_services=True` / `scheduler=True` to `create_context()` when you want
them (a worker, below); `app.context()` can only keep off what the app was
built without.

---

## Declare commands

A command is a method on any provider, marked with `@command(name)`. Its
parameters become the CLI: dependencies are injected, the rest are parsed from
the command line with the standard library's `argparse`.

<!-- docs-contract: run -->
```python
import enum
from pathlib import Path
from typing import Annotated

from lauren import injectable, module
from lauren.commands import CommandContext, Opt, command


class Fixture(enum.Enum):
    DEMO = "demo"
    LOAD = "load-test"


@injectable()
class SeedCommands:
    def __init__(self, db: Database) -> None:
        self._db = db

    @command("db:seed", help="Insert fixture data.", aliases=("seed",))
    async def seed(
        self,
        ctx: CommandContext,
        fixture: Fixture = Fixture.DEMO,
        *,
        count: Annotated[int, Opt(short="-n", help="rows per table", env="SEED_COUNT")] = 50,
        reset: bool = False,
    ) -> int:
        if reset and not ctx.confirm("Truncate every table?"):
            return 1
        self._db.users.extend(f"{fixture.value}-{i}" for i in range(count))
        ctx.echo(f"seeded {count} rows")
        return 0

    @command("db:export")
    async def export(self, target: Path, *, tables: list[str] | None = None) -> dict:
        """Export tables to a JSON file."""
        return {"target": target.name, "tables": tables or []}


@module(providers=[Database, DbSession, UserService, SeedCommands])
class ToolsModule:
    pass


async def seed_and_export() -> None:
    async with LaurenFactory.create_context(ToolsModule) as ctx:
        seeded = await ctx.run_command("db:seed", "load-test", "-n", "2")
        assert (seeded.exit_code, seeded.stdout) == (0, "seeded 2 rows\n")

        exported = await ctx.run_command("db:export", "out.json", "--tables", "users", "--tables", "orders")
        assert exported.stdout == '{"target": "out.json", "tables": ["users", "orders"]}\n'

        refused = await ctx.run_command("db:seed", "--reset")  # nobody can answer the prompt
        assert refused.exit_code == 1 and "refused" in refused.stderr


asyncio.run(seed_and_export())
```

How parameters map — once, at startup:

| Parameter | Becomes |
|---|---|
| `ctx: CommandContext` | injected |
| a type the provider's module can resolve, or `Annotated[T, Inject(token)]` | injected from the command's scope |
| `bool`, wherever it is declared | `--reset / --no-reset` |
| keyword-only (after `*`) | `--kebab-case` option, required without a default |
| other parameters | positional arguments, optional when they have a default |
| `list[T]` / `tuple[T, ...]` | a repeatable option (`--tables a --tables b`), or a variadic positional |
| `T \| None` | optional, default `None` |
| `Enum` / `Literal[...]` | choices |

CLI types are `str`, `int`, `float`, `Decimal`, `bool`, `Path`, `UUID`,
`datetime`, `date`, `time`, `timedelta`, enums and literals.
`Annotated[T, Opt(short="-n", env="SEED_COUNT", help="...")]` adds a short
flag, an environment-variable fallback and help text to an option. Help is
generated from the decorator's `help=` (or the docstring's first line), the
`Opt` help, defaults, choices and env names: `db:seed --help`.

Everything is checked in `LaurenFactory.create`, so a broken command fails
the server's startup too:

| Mistake | Error |
|---|---|
| `@command` without parentheses, a name outside `^[a-z0-9][a-z0-9._:-]*$` | `DecoratorUsageError` (at decoration) |
| two commands share a name or alias | `DuplicateBindingError` (`reason="duplicate_command_name"`) |
| a parameter that is neither injectable nor a CLI type — e.g. a provider another module does not export | `UnresolvableParameterError` (`reason="unsupported_command_parameter"`) |
| `Opt(short=...)` / `Opt(env=...)` on a positional parameter | `DecoratorUsageError` (`reason="opt_on_positional"`) |

Each invocation runs in its own operation scope seeded with the
`CommandContext`, so a request-scoped dependency (or a request-scoped command
class) is created for the run and torn down after it — `@pre_destruct`, then
`aclose()`. Sync commands run in a worker thread. Keep commands thin: like a
controller, a command adapts input and output and delegates to services.

### Output and exit codes

Write output through the context — `ctx.echo(...)` to stdout, `ctx.error(...)`
to stderr — so the runner and tests can capture it. The return value decides
the exit code:

| The command… | Exit code | Output |
|---|---|---|
| returns `None` | 0 | |
| returns an `int` | that int | |
| returns a `bool` | 0 if true, else 1 | |
| returns anything else | 0 | printed as JSON with the app's encoder |
| calls `ctx.fail("message", exit_code=2)` | 2 | the message on stderr |
| gets invalid arguments, or the name is unknown | 2 | usage on stderr |
| raises | 1 | a one-line summary on stderr (the traceback with `--verbose`) |

`ctx.confirm("Truncate every table?")` asks on a terminal, answers yes under
`--yes`, and refuses (returns `False`, with a note on stderr) when nobody can
answer — it never hangs a `CronJob`.

---

## Run them: `python -m lauren`

```console
$ python -m lauren commands app.main:AppModule
commands:
  db:export       Export tables to a JSON file.
  db:seed (seed)  Insert fixture data.

$ python -m lauren exec app.main:AppModule db:seed load-test -n 500 --reset --yes
seeded 500 rows

$ export LAUREN_TARGET=app.main:create_app
$ python -m lauren exec db:export out.json --tables users --tables orders
{"target": "out.json", "tables": ["users", "orders"]}
```

`TARGET` is `pkg.module:attr` — or `--target`, or `$LAUREN_TARGET` — and may
name a `@module` class, a `LaurenApp` (`main:app`), a `Lauren()` app, an
`ApplicationContext`, or a zero-argument factory (sync or async) returning one
of them (`main:create_app`).

`exec` parses the command's arguments **before** starting the app, so a typo
or `--help` never opens database connections. Then it starts the app with
hosted services and the scheduler off, runs the command in a fresh scope, and
shuts down. Exit codes are the table above, plus `3` when the application
cannot be imported, built or started (the `StartupError` is printed with its
`detail`), and `130` / `143` for SIGINT / SIGTERM: the first signal cancels
the command (its `finally` blocks run), the scope is torn down and the app
shuts down; a second signal exits immediately.

`console` opens a Python REPL on the started app — `ctx`, `app`, `container`,
`get`, `resolve`, `scope` and `asyncio` are preloaded and top-level `await`
works:

```console
$ python -m lauren console app.main:AppModule
lauren console: app.main:AppModule (started; hosted services and the scheduler are off)
>>> from app.users import UserService
>>> await get(UserService).count()
1830
>>> exit()
```

The console is a local operator tool with the app's full privileges: point it
at read-only credentials when you inspect production.

The runner needs nothing beyond `lauren`. The `lauren-cli` package adds
`lauren exec`, `lauren commands` and `lauren console` with rich help, shell
completion, IPython and automatic target detection; they call the same core
runner (`lauren._commands._runner.main`, or `load_context()` plus the
`ApplicationContext` API) in-process, so behaviour and exit codes match.

---

## Test commands

`ctx.run_command()` behaves like `python -m lauren exec` and captures the
output. Replace dependencies with `Test.create_testing_module(...)` — its
overrides apply to contexts exactly as to apps:

<!-- docs-contract: run -->
```python
from lauren.testing import Test


class FakeDatabase:
    def __init__(self) -> None:
        self.users: list[str] = []


async def test_seed_command() -> None:
    app = (
        Test.create_testing_module(ToolsModule)
        .override_provider(Database)
        .use_value(FakeDatabase())
        .compile()
    )
    async with app.context() as ctx:
        result = await ctx.run_command("db:seed", "--count", "3")
        assert ctx.get(Database).users == ["demo-0", "demo-1", "demo-2"]
    assert result.exit_code == 0
    assert "seeded 3 rows" in result.stdout


asyncio.run(test_seed_command())
```

`CommandResult` has `exit_code`, `value` (the return value), `stdout`,
`stderr` and `exception`. Pass `assume_yes=True` (or `"--yes"`) to confirm
prompts, or `interactive=True, stdin=io.StringIO("y\n")` to answer them.
Build the app with `strict_request_scope=True` in tests to prove nothing
resolves request-scoped providers outside a scope.

---

## Workers

A worker process wants hosted services (or the scheduler) but no HTTP:

<!-- docs-contract: fragment -->
```python
async def worker_main() -> None:
    async with LaurenFactory.create_context(WorkerModule, hosted_services=True) as ctx:
        await ctx.wait_for_shutdown_signal()  # SIGINT / SIGTERM → leave the block


asyncio.run(worker_main())
```

`wait_for_shutdown_signal()` installs SIGINT / SIGTERM handlers for the call
only and returns the signal number; leaving the block then performs the
graceful shutdown (hosted services stop, `on_shutdown`, `@pre_destruct`).

---

## Periodic work in Kubernetes

For heavy periodic work prefer a `CronJob` running a command over an
in-process `@cron` job: it runs once per schedule however many web replicas
you have, with its own resources and logs.

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: purge-sessions
spec:
  schedule: "0 3 * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: purge
              image: registry.example.com/shop:latest
              command: ["python", "-m", "lauren", "exec", "app.main:AppModule", "sessions:purge", "--yes"]
```

A non-zero exit code fails the Job, so Kubernetes' retry and alerting apply.

---

## Replace an admin endpoint

Before — anyone who can reach the API can re-seed the database:

<!-- docs-contract: fragment -->
```python
@controller("/api")
class SeedController:
    @post("/seed")
    async def seed(self, db: DatabaseService) -> dict:
        return {"rows": await run_seed(db)}
```

After — the same service, run by an operator:

<!-- docs-contract: fragment -->
```python
@injectable()
class SeedCommands:
    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    @command("db:seed", help="Insert the demo data.")
    async def seed(self, ctx: CommandContext, *, reset: bool = False) -> int:
        if reset and not ctx.confirm("Delete existing data first?"):
            return 1
        ctx.echo(f"seeded {await run_seed(self._db, reset=reset)} rows")
        return 0
```

List `SeedCommands` in the module's `providers` and delete the controller.
Test fixtures that called private `_open()` hooks by hand can use a context
instead: `async with app.context() as ctx:` runs the real lifecycle.

---

## See also

- [Dependency Injection → Scopes outside requests](dependency-injection.md#part-g-scopes-outside-requests-appscope) — operation scopes and `strict_request_scope`.
- [Scheduling & Hosted Services](scheduling.md) — in-process periodic work and long-running services.
- [Testing](testing.md) — `Test.create_testing_module` overrides.
- [Reference → Contexts & Commands](../reference/commands.md) — the API.
