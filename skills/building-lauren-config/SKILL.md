---
name: building-lauren-config
description: Adds typed, startup-validated configuration to a Lauren app. Covers settings classes (frozen dataclasses, pydantic models, msgspec Structs, pydantic-settings), ConfigModule.for_root / for_feature, .env / TOML / YAML / secret-file / environment sources and their precedence, env-variable naming, profiles (LAUREN_PROFILE, Profile injection), Secret[T] redaction, load_settings for factory kwargs, async secret-manager sources, the aggregated ConfigValidationError, and testing with environ= / overrides=. Use whenever code would otherwise read os.environ, call load_dotenv, or hand-roll a ConfigService.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.


# Lauren Typed Configuration

Configuration is loaded and validated **inside `LaurenFactory.create`**. One
`ConfigValidationError` lists every missing or invalid key with its source.
Never read `os.environ` in `__init__` or at import time; never
`load_dotenv(override=True)`.

## Declare settings

```python
from dataclasses import dataclass, field
from typing import Annotated, Literal

from lauren.config import Env, Secret


@dataclass(frozen=True)
class DatabaseSettings:
    url: str                       # required -> APP_DATABASE__URL
    pool_size: int = 10            # APP_DATABASE__POOL_SIZE


@dataclass(frozen=True)
class AppSettings:
    session_secret: Secret[str]    # required, redacted everywhere
    database: DatabaseSettings     # nested section ("__" delimiter)
    debug: bool = False            # 1/0 true/false yes/no on/off; anything else is an error
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = "INFO"
    allowed_origins: list[str] = field(default_factory=list)          # "a, b" or '["a", "b"]'
    llm_api_key: Annotated[Secret[str], Env("LLM_API_KEY")] = Secret("")  # explicit name, no prefix
```

Pydantic `BaseModel`, `msgspec.Struct` and `pydantic_settings.BaseSettings`
(delegated) also work. TypedDict does not.

## Register and inject

```python
from lauren import injectable, module
from lauren.config import ConfigModule, Profile


@module(imports=[ConfigModule.for_root(AppSettings, env_prefix="APP_")])
class AppModule:
    pass


@injectable()
class Mailer:
    def __init__(self, settings: AppSettings, profile: Profile) -> None:
        self.sandbox = not profile.is_production
```

- `for_root` is **global** (any module injects `AppSettings`); its `profile=`,
  `profiles=`, `environ=` are app-wide.
- `for_feature(Settings, env_prefix="DB_", section="database")` is
  **module-scoped**: only the importing module (or importers of a module that
  re-exports it) can inject it; it inherits the root's sources.
- `expose_sections=True` also provides nested sections (`DatabaseSettings`).

## Sources and precedence (later wins)

class defaults < `sources` in order < `EnvSource()` (must be last) < `overrides=`.
Default `sources=None` = `.env` (optional), `.env.{profile}` (optional),
environment.

```python
from lauren.config import DotenvSource, EnvSource, SecretsDirSource, TomlSource

ConfigModule.for_root(
    AppSettings,
    env_prefix="APP_",
    sources=[
        TomlSource("config/settings.toml", required=False),           # [database] url = "..."
        TomlSource("config/settings.{profile}.toml", required=False),
        DotenvSource(".env", required=False),
        SecretsDirSource("/run/secrets", required=False),              # one file per key
        EnvSource(),
    ],
)
```

`YamlSource` needs `lauren[yaml]`. TOML/YAML values are checked strictly;
strings from env/`.env`/secret files are coerced (int, float, Decimal, bool,
Enum, Literal, Path, UUID, ISO dates, `timedelta` seconds or `PT5M`, lists,
JSON dicts, `X | None` with `""` → `None`, `Secret[T]`).

## Profiles

`for_root(profile=...)` > `LAUREN_PROFILE` > `"development"`, validated
against `profiles=("development", "test", "production")`. Unknown profile →
`ConfigValidationError` (`reason="unknown_profile"`). `{profile}` in paths is
substituted.

## Values needed before the factory

```python
from lauren import LaurenFactory, SessionConfig, use_value
from lauren.config import load_settings

settings = load_settings(AppSettings, env_prefix="APP_")    # same aggregated error
app = LaurenFactory.create(
    AppModule,
    sessions=SessionConfig(secret=settings.session_secret),  # Secret accepted
    global_providers=[use_value(provide=AppSettings, value=settings)],
)
```

(Do not also import `ConfigModule.for_root(AppSettings)` then — the token would
be registered twice: `DuplicateBindingError`.)

## Async secret managers

```python
class VaultSource:
    name = "vault"

    async def load(self, keys):              # every env name the class can read
        return await read_from_vault(keys)   # Mapping[str, str]

ConfigModule.for_root(AppSettings, env_prefix="APP_", sources=[VaultSource(), EnvSource()])
```

Resolved at the start of `app.startup()` (lifespan / `TestClient`), before
`@post_construct`; errors raise the same `ConfigValidationError` there.

## Testing

```python
from lauren.testing import TestClient

@module(
    imports=[ConfigModule.for_root(
        AppSettings,
        env_prefix="APP_",
        sources=[EnvSource()],                         # skip .env files
        environ={"APP_SESSION_SECRET": "x", "APP_DATABASE__URL": "sqlite://"},
        overrides={"debug": True},                     # dotted paths
    )],
)
class TestAppModule:
    pass

client = TestClient(LaurenFactory.create(TestAppModule))
```

Startup-failure tests: `with pytest.raises(ConfigValidationError) as exc:` and
assert on `exc.value.detail["errors"]` (`settings`, `field`, `key`, `reason`,
`source`, `searched`).

## Pitfalls

- ❌ `os.environ[...]` in `__init__` or at import time — fails late, one key at a time.
- ❌ `load_dotenv(override=True)` — a `.env` must never override the environment.
- ❌ Logging a raw secret — wrap it in `Secret[T]`; call `get_secret_value()` only where needed.
- ❌ `EnvSource()` anywhere but last — rejected with `DecoratorUsageError`.
- ❌ Expecting runtime reload — settings are immutable for the app's lifetime.
