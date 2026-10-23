# Typed Configuration

Configuration is the input most likely to break a deploy, so Lauren treats it
like everything else: it is declared up front, validated inside
`LaurenFactory.create(...)`, and a mistake fails startup with **one** error
that lists every missing or invalid value, with the key and the source of each.
Settings are ordinary classes injected through DI; secrets are redacted
everywhere; the process environment is read but never written.

---

## Minimal example

Declare settings as a frozen dataclass (no extra dependency), import a
`ConfigModule`, and inject the settings class wherever you need it.

<!-- docs-contract: run -->
```python
from dataclasses import dataclass, field
from typing import Annotated, Literal

from lauren import LaurenFactory, controller, get, injectable, module
from lauren.config import ConfigModule, Env, EnvSource, Profile, Secret


@dataclass(frozen=True)
class DatabaseSettings:
    url: str                                   # required
    pool_size: int = 10


@dataclass(frozen=True)
class AppSettings:
    session_secret: Secret[str]                # required, redacted everywhere
    database: DatabaseSettings                 # nested section
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = "INFO"
    allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:3000"])
    llm_api_key: Annotated[Secret[str], Env("LLM_API_KEY")] = Secret("")  # explicit name


@injectable()
class Mailer:
    def __init__(self, settings: AppSettings, profile: Profile) -> None:   # typed injection
        self.debug = settings.debug
        self.sandbox = not profile.is_production


@controller("/status")
class StatusController:
    def __init__(self, mailer: Mailer, settings: AppSettings) -> None:
        self.mailer = mailer
        self.settings = settings

    @get("/")
    async def status(self) -> dict:
        return {
            "debug": self.mailer.debug,
            "pool_size": self.settings.database.pool_size,
            "origins": self.settings.allowed_origins,
        }
```

In production the root module simply imports `ConfigModule.for_root(...)`; the
values come from `.env`, `.env.{profile}` and the process environment:

```python
@module(
    imports=[ConfigModule.for_root(AppSettings, env_prefix="APP_")],
    controllers=[StatusController],
    providers=[Mailer],
)
class AppModule:
    pass
```

`APP_SESSION_SECRET`, `APP_DATABASE__URL`, `APP_DATABASE__POOL_SIZE`,
`APP_DEBUG`, `APP_LOG_LEVEL`, `APP_ALLOWED_ORIGINS` and `LLM_API_KEY` are read
and coerced to the declared types. A test pins the environment instead of
touching `os.environ`:

<!-- docs-contract: run -->
```python
from lauren.testing import TestClient


@module(
    imports=[
        ConfigModule.for_root(
            AppSettings,
            env_prefix="APP_",
            sources=[EnvSource()],              # no .env files in this test
            environ={                           # replaces os.environ for this app
                "APP_SESSION_SECRET": "s3cr3t",
                "APP_DATABASE__URL": "postgresql://db/app",
                "APP_DATABASE__POOL_SIZE": "5",
                "APP_ALLOWED_ORIGINS": "https://a.example, https://b.example",
            },
        )
    ],
    controllers=[StatusController],
    providers=[Mailer],
)
class TestAppModule:
    pass


app = LaurenFactory.create(TestAppModule)
assert TestClient(app).get("/status/").json() == {
    "debug": False,
    "pool_size": 5,
    "origins": ["https://a.example", "https://b.example"],
}
```

---

## What fails, and how

Every problem in every settings class is collected first and raised together as
one [`ConfigValidationError`](../reference/errors.md) (a `StartupError`) — by
`create()`, before any server starts:

<!-- docs-contract: run -->
```python
from lauren.exceptions import ConfigValidationError

@module(
    imports=[
        ConfigModule.for_root(
            AppSettings,
            env_prefix="APP_",
            sources=[EnvSource()],
            environ={"APP_DATABASE__POOL_SIZE": "ten", "APP_DEBUG": "maybe", "LAUREN_PROFILE": "production"},
        )
    ],
)
class BrokenModule:
    pass


try:
    LaurenFactory.create(BrokenModule)
except ConfigValidationError as exc:
    reasons = [(e["field"], e["reason"]) for e in exc.detail["errors"]]
    assert reasons == [
        ("session_secret", "missing"),
        ("database.url", "missing"),
        ("database.pool_size", "invalid"),
        ("debug", "invalid"),
    ]
```

```text
4 configuration error(s) in profile 'production':
  - AppSettings.session_secret  [APP_SESSION_SECRET]  missing (searched: environment)
  - AppSettings.database.url  [APP_DATABASE__URL]  missing (searched: environment)
  - AppSettings.database.pool_size  [APP_DATABASE__POOL_SIZE]  invalid int 'ten' (from environment)
  - AppSettings.debug  [APP_DEBUG]  invalid bool 'maybe': use one of 1/0, true/false, yes/no, on/off (from environment)
```

`detail` is a stable, machine-readable contract for deploy tooling:
`{"profile": ..., "errors": [...]}`. Each entry has a `reason`:

| `reason` | Keys | Meaning |
|---|---|---|
| `missing` | `settings`, `field`, `key`, `searched` | A required field has no value in any source. |
| `invalid` | `settings`, `field`, `key`, `expected`, `source`, `value`, `message` | A value could not be coerced or failed the model's validation. `value` is omitted for secret fields. |
| `unknown_field` | `settings`, `field`, `source` | An `overrides=` key names no field. |
| `unknown_profile` | `profile`, `allowed`, `source` | The active profile is not in `profiles=`. |
| `missing_file` / `parse_error` / `source_error` | `source`, `message` (`line` for `.env`) | A required file is absent, malformed, or a source failed. |

Errors from a `for_feature` module also carry `module` — the module that
imports it. Problems in the code rather than the configuration (an unsupported
settings class, `EnvSource()` not listed last, …) raise `DecoratorUsageError`
when the `ConfigModule` is declared.

---

## Sources and precedence

Lowest to highest — later wins:

1. Class field defaults.
2. Sources, in the order listed.
3. `EnvSource()` — the process environment, or the `environ=` mapping. It must
   be the **last** source, so a `.env` file can never override a real
   environment variable.
4. `overrides={...}`, keyed by dotted field path (`{"database.url": "sqlite://"}`).

`sources=None` means `[DotenvSource(".env", required=False),
DotenvSource(".env.{profile}", required=False), EnvSource()]`.

| Source | Reads | Values |
|---|---|---|
| `EnvSource()` | `os.environ` or `environ=` | strings (lax coercion) |
| `DotenvSource(path, *, required=True, encoding="utf-8")` | `KEY=VALUE` lines: `export` prefix, `#` comments, `'literal'` and `"escaped\n"` (multi-line) values; no `${VAR}` interpolation | strings |
| `SecretsDirSource(path, *, required=True)` | one file per key (`/run/secrets/APP_DB_PASSWORD`, or its lower-case name); trailing newlines stripped | strings |
| `TomlSource(path, *, required=True)` | stdlib `tomllib`; tables map to nested sections | typed (strict) |
| `YamlSource(path, *, required=True)` | PyYAML (`pip install 'lauren[yaml]'`); mappings are sections | typed (strict) |
| an `AsyncSource` | see [Async sources](#async-sources) | strings |

`{profile}` in any path is replaced by the active profile. Relative paths
resolve against the working directory at `create()` time. Unknown keys in
files are ignored (one file can serve several settings classes).

**The environment is never written.** `DotenvSource` parses into a private
mapping — unlike `load_dotenv(override=True)` — so two apps with different
`.env` files or `environ=` mappings coexist in one process.

### Environment-variable names

| Field | Name |
|---|---|
| `debug` with `env_prefix="APP_"` | `APP_DEBUG` |
| `database.url` with `env_prefix="APP_"` | `APP_DATABASE__URL` (nested delimiter `__`, as in pydantic-settings) |
| `Annotated[str, Env("DATABASE_URL", "DB_URL")]` | the first present of `DATABASE_URL`, `DB_URL` — no prefix |

Generated names are upper case and matched exactly. In TOML / YAML the same
field is `debug = true` and `[database] url = "..."`.

### String coercion

Values from the environment, `.env` and secret files are strings and go through
a deliberate **lax** table; TOML / YAML values are already typed and are
checked **strictly** (no `"10"` → `10`):

| Target | Accepted strings |
|---|---|
| `str` | anything |
| `int` | `int(s)` (underscores allowed) |
| `float`, `Decimal` | `float(s)` / `Decimal(s)` |
| `bool` | `1/0`, `true/false`, `yes/no`, `on/off` (any case) — anything else is **invalid** |
| `Enum` | by value, then by name |
| `Literal[...]` | exact match |
| `Path`, `UUID` | constructor |
| `datetime`, `date`, `time` | ISO 8601 |
| `timedelta` | seconds, or an ISO 8601 duration (`PT5M`) |
| `list[T]`, `tuple[T, ...]`, `set[T]` | a JSON array if it starts with `[`, else comma-separated, items trimmed |
| `dict[str, T]` | a JSON object |
| `X \| None` | the empty string is `None` |
| `Secret[T]` | coerced as `T`, then wrapped |
| nested settings class | from `__`-delimited names, or a TOML / YAML table |

---

## Settings classes

| Shape | How it is validated |
|---|---|
| `@dataclass` (frozen recommended) | the coercion table above, recursively for nested dataclasses |
| `pydantic.BaseModel` | coerced values, then `model_validate` — constraints and validators run, errors are mapped back to keys and sources |
| `msgspec.Struct` | coerced values, then `msgspec.convert(..., strict=False)` (msgspec reports its first error) |
| `pydantic_settings.BaseSettings` | **delegated**: pydantic-settings reads its own sources; Lauren calls it inside `create()` and maps its errors. `env_prefix=`, `sources=` and `section=` are rejected — configure its `model_config` |

TypedDict is not supported (no defaults, no immutability). A section typed
`X | None = None` (or with a default) keeps its default when none of its keys
is set. A mutable dataclass logs a startup warning — declare settings
`@dataclass(frozen=True)`.

---

## `ConfigModule`

<!-- docs-contract: fragment -->
```python
ConfigModule.for_root(settings_cls, *, env_prefix="", sources=None, section=None,
                      profile=None, profiles=("development", "test", "production"),
                      environ=None, overrides=None, is_global=True, expose_sections=False)
ConfigModule.for_feature(settings_cls, *, env_prefix="", sources=None, section=None,
                         overrides=None, expose_sections=False)
```

Each call returns a new `@module` class (like `StaticFilesModule.for_root`).

- **`for_root`** is global by default: every module can inject the settings
  class without importing anything. Its `profile=`, `profiles=` and `environ=`
  are app-wide and also apply to feature settings.
- **`for_feature`** is module-scoped: the importing module (and importers of a
  module that re-exports the class) can inject it. With `sources=None` it
  inherits the root's sources. `section="database"` reads the `[database]`
  table of TOML / YAML files.
- **`expose_sections=True`** also provides every nested section class
  (`DatabaseSettings` inside `AppSettings`) as its own token. A section type
  used twice is rejected.

<!-- docs-contract: run -->
```python
@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int = 25


@injectable()
class MailClient:
    def __init__(self, settings: MailSettings) -> None:
        self.address = f"{settings.host}:{settings.port}"


@module(
    imports=[ConfigModule.for_feature(MailSettings, env_prefix="MAIL_")],
    providers=[MailClient],
    exports=[MailClient],
)
class MailModule:
    pass


@module(
    imports=[
        ConfigModule.for_root(
            AppSettings, env_prefix="APP_", sources=[EnvSource()],
            environ={"APP_SESSION_SECRET": "x", "APP_DATABASE__URL": "sqlite://", "MAIL_HOST": "smtp.local"},
        ),
        MailModule,
    ],
)
class MailApp:
    pass


mail_app = LaurenFactory.create(MailApp)
```

The `Lauren` imperative API takes the same module:
`app.include_module(ConfigModule.for_root(AppSettings))`.

---

## Profiles

The active profile comes from `for_root(profile=...)`, else the
`LAUREN_PROFILE` variable (read from `environ=` when one is given), else
`"development"`. It must be one of `profiles=` — an unknown profile is a
`ConfigValidationError`, never a silent fallback. Inject `Profile` (frozen:
`name`, `is_development`, `is_test`, `is_production`) to branch on it, and use
`{profile}` in source paths to layer per-environment files:

```python
ConfigModule.for_root(
    AppSettings,
    env_prefix="APP_",
    sources=[
        TomlSource("config/settings.toml", required=False),
        TomlSource("config/settings.{profile}.toml", required=False),
        DotenvSource(".env", required=False),
        DotenvSource(".env.{profile}", required=False),
        EnvSource(),
    ],
)
```

Coming from an `APP_ENV` variable? Rename it to `LAUREN_PROFILE` in your
deployment; custom profile names (`staging`, `qa`) go in `profiles=`.

---

## Secrets

`Secret[T]` wraps a value so it never shows itself: `repr`, `str`, f-strings,
log lines, JSON responses and error messages print `**********`. Read it
explicitly with `get_secret_value()`. Equality and hashing compare the
underlying values; truthiness follows the value.

- Lauren's JSON encoders render `Secret` (and pydantic `SecretStr` /
  `SecretBytes`) as `"**********"`.
- `Secret[T]` works as a pydantic model field (validated as `T`, masked in
  JSON mode).
- `SessionConfig(secret=...)` accepts `Secret[str]` / `Secret[bytes]` (or a list
  of them, for rotation) and unwraps it internally.
- `ConfigValidationError` omits the `value` of secret fields.

---

## Values needed before the factory: `load_settings`

Factory kwargs such as `sessions=`, `docs_url=` or `logger=` need values before
`LaurenFactory.create` runs. `load_settings` uses the same loader and raises
the same aggregated error:

<!-- docs-contract: run -->
```python
from lauren import SessionConfig, use_value
from lauren.config import load_settings

settings = load_settings(
    AppSettings,
    env_prefix="APP_",
    sources=[EnvSource()],
    environ={"APP_SESSION_SECRET": "a-long-random-secret", "APP_DATABASE__URL": "sqlite://"},
)

@controller("/debug")
class DebugController:
    def __init__(self, settings: AppSettings) -> None:     # the use_value below provides it
        self.settings = settings

    @get("/")
    async def show(self) -> dict:
        return {"debug": self.settings.debug}


@module(controllers=[DebugController])
class FactoryKwargsModule:
    pass

app = LaurenFactory.create(
    FactoryKwargsModule,
    sessions=SessionConfig(secret=settings.session_secret),      # Secret accepted
    docs_url="/docs" if settings.debug else None,
    global_providers=[use_value(provide=AppSettings, value=settings)],
)
```

`load_settings(settings_cls, *, env_prefix="", sources=None, section=None,
profile=None, profiles=..., environ=None, overrides=None)`; its async twin
`aload_settings(...)` also awaits async sources. Prefer `ConfigModule` for
anything injectable — it is the only form reusable feature modules can use.

---

## Async sources

Secret managers (Vault, AWS Secrets Manager, …) are asynchronous, but
`LaurenFactory.create` is not. Implement the `AsyncSource` protocol:

```python
class VaultSource:
    name = "vault"                      # used in errors and the startup log

    async def load(self, keys):         # every env name the settings class can read
        secrets = await vault_client.read("kv/app")
        return {k: v for k, v in secrets.items() if k in keys}


ConfigModule.for_root(AppSettings, env_prefix="APP_", sources=[VaultSource(), EnvSource()])
```

Settings fed by an async source are completed at the start of
`LaurenApp.startup()` (the ASGI lifespan, or `TestClient`), before any
`@post_construct` hook — the one documented exception to "validated in
`create()`". `create()` still reports every problem an async source could not
fix (for example an invalid value in `EnvSource()`, which outranks it); the
rest raises the same `ConfigValidationError` from `startup()`. Resolving the
settings before startup raises `StartupError` (`reason="settings_not_loaded"`).

---

## Observability

`create()` logs one INFO line (context `Config`) with the profile, the
settings classes, their key counts and the sources that were actually read —
never values:

```text
[Config] profile=production settings=AppSettings(8 keys) MailSettings(2 keys) sources=.env.production, environment
```

---

## Testing

- Pass `environ={...}` (and `sources=[EnvSource()]` to skip `.env` files) to
  `for_root` — every settings class in the app reads it; `os.environ` is never
  consulted or modified.
- `overrides={"database.url": "sqlite://"}` pins individual values (strings are
  coerced like environment values; other values must already have the field's
  type; a whole section instance is accepted too).
- Build several apps with different configuration in one test process — each
  `create()` call loads its own settings.

---

## Compared with

| | Lauren | pydantic-settings | NestJS `@nestjs/config` |
|---|---|---|---|
| Validated | inside `LaurenFactory.create` | when the object is constructed | only with a `validate` / schema option |
| Errors | all at once, with key and source | all fields of one class | first failing schema |
| Access | typed DI injection | an object you pass around | `configService.get("KEY")` |
| Secrets | `Secret[T]`, redacted in JSON / logs / errors | `SecretStr` | none |
| `.env` | parsed privately; environment always wins | `env_file`, environment wins | `dotenv`, loaded into `process.env` |
| Scoping | global root + module-scoped `for_feature` | — | `forRoot` / `forFeature` |
| Pydantic | optional (dataclasses work) | required | n/a |

!!! tip "lauren-cli"
    `lauren dev` loads `.env` into the child process environment. With
    `ConfigModule` this is harmless: the process environment wins either way and
    re-reading the same file yields the same values, so `lauren run`, plain
    `uvicorn` and containers all see identical configuration.
