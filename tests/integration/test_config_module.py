"""Integration tests for ``ConfigModule`` — typed settings validated inside ``LaurenFactory.create``."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lauren import (
    Lauren,
    LaurenFactory,
    Session,
    SessionConfig,
    controller,
    get,
    injectable,
    module,
    use_value,
)
from lauren.config import (
    ConfigModule,
    DotenvSource,
    EnvSource,
    Profile,
    Secret,
    TomlSource,
    load_settings,
)
from lauren.exceptions import (
    ConfigValidationError,
    DecoratorUsageError,
    DuplicateBindingError,
    MissingProviderError,
    ModuleExportViolation,
    StartupError,
)
from lauren.logging import InMemoryLogger, LogLevel
from lauren.testing import TestClient

# ---------------------------------------------------------------------------
# Settings classes and consumers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseSettings:
    url: str
    pool_size: int = 10


@dataclass(frozen=True)
class AppSettings:
    session_secret: Secret[str]
    database: DatabaseSettings
    debug: bool = False


@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int = 25


@dataclass(frozen=True)
class FeatureFlags:
    beta: bool = False


@dataclass
class MutableSettings:
    value: str = "x"


@injectable()
class Mailer:
    def __init__(self, settings: AppSettings, profile: Profile) -> None:
        self.debug = settings.debug
        self.sandbox = not profile.is_production


@injectable()
class Database:
    def __init__(self, settings: MailSettings) -> None:
        self.settings = settings


@module(
    imports=[ConfigModule.for_feature(MailSettings, env_prefix="MAIL_")],
    providers=[Database],
    exports=[Database],
)
class MailModule:
    pass


@module(providers=[Mailer], exports=[Mailer])
class NotificationsModule:
    """Injects AppSettings without importing any ConfigModule (global root)."""


@controller("/info")
class InfoController:
    def __init__(self, mailer: Mailer, settings: AppSettings) -> None:
        self.mailer = mailer
        self.settings = settings

    @get("/")
    async def info(self) -> dict[str, Any]:
        return {
            "debug": self.mailer.debug,
            "sandbox": self.mailer.sandbox,
            "pool": self.settings.database.pool_size,
            "secret": self.settings.session_secret,
        }


GOOD_ENV = {"APP_SESSION_SECRET": "s3cr3t", "APP_DATABASE__URL": "sqlite://", "MAIL_HOST": "smtp"}


def _app_module(environ: dict[str, str], **root_kwargs: Any) -> type:
    root_kwargs.setdefault("sources", [EnvSource()])

    @module(
        imports=[
            ConfigModule.for_root(AppSettings, env_prefix="APP_", environ=environ, **root_kwargs),
            NotificationsModule,
            MailModule,
        ],
        controllers=[InfoController],
    )
    class AppModule:
        pass

    return AppModule


# ---------------------------------------------------------------------------
# Validation happens in create()
# ---------------------------------------------------------------------------


def test_valid_configuration_is_injected_and_secrets_stay_masked() -> None:
    app = LaurenFactory.create(_app_module({**GOOD_ENV, "APP_DEBUG": "true", "APP_DATABASE__POOL_SIZE": "3"}))

    response = TestClient(app).get("/info/")

    assert response.status_code == 200
    assert response.json() == {"debug": True, "sandbox": True, "pool": 3, "secret": "**********"}


def test_create_raises_one_error_listing_every_problem_across_modules() -> None:
    environ = {"APP_DATABASE__POOL_SIZE": "ten", "LAUREN_PROFILE": "production"}

    with pytest.raises(ConfigValidationError) as excinfo:
        LaurenFactory.create(_app_module(environ))

    detail = excinfo.value.detail
    assert detail["profile"] == "production"
    assert [(e["settings"], e["field"], e["reason"]) for e in detail["errors"]] == [
        ("AppSettings", "session_secret", "missing"),
        ("AppSettings", "database.url", "missing"),
        ("AppSettings", "database.pool_size", "invalid"),
        ("MailSettings", "host", "missing"),
    ]
    assert detail["errors"][3]["module"] == "MailModule"
    assert detail["errors"][3]["key"] == "MAIL_HOST"
    assert "(declared by MailModule)" in str(excinfo.value)
    assert "module" not in detail["errors"][0]


def test_configuration_error_is_not_deferred_to_startup() -> None:
    app_module = _app_module({})

    with pytest.raises(ConfigValidationError):
        LaurenFactory.create(app_module)


def test_unknown_profile_fails_create() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        LaurenFactory.create(_app_module({**GOOD_ENV, "LAUREN_PROFILE": "staging"}))

    (entry,) = excinfo.value.detail["errors"]
    assert entry["reason"] == "unknown_profile"
    assert entry["key"] == "LAUREN_PROFILE"


def test_explicit_profile_and_custom_profiles() -> None:
    app = LaurenFactory.create(_app_module(GOOD_ENV, profile="qa", profiles=("qa", "production")))

    assert asyncio.run(app.container.resolve(Profile)) == Profile("qa")


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def test_for_root_is_global() -> None:
    app = LaurenFactory.create(_app_module(GOOD_ENV))

    mailer = asyncio.run(app.container.resolve(Mailer, owning_module=NotificationsModule))

    assert mailer.debug is False


def test_for_feature_is_visible_to_its_importer() -> None:
    app = LaurenFactory.create(_app_module(GOOD_ENV))

    database = asyncio.run(app.container.resolve(Database, owning_module=MailModule))

    assert database.settings == MailSettings(host="smtp")


def test_for_feature_is_scoped_to_importers() -> None:
    @injectable()
    class Outsider:
        def __init__(self, settings: MailSettings) -> None:
            self.settings = settings

    @module(providers=[Outsider], exports=[Outsider])
    class OutsiderModule:
        pass

    @module(
        imports=[
            ConfigModule.for_root(AppSettings, environ=GOOD_ENV, env_prefix="APP_"),
            MailModule,
            OutsiderModule,
        ]
    )
    class AppModule:
        pass

    with pytest.raises(MissingProviderError, match="visible"):
        LaurenFactory.create(AppModule)


def test_feature_settings_can_be_re_exported() -> None:
    @module(imports=[ConfigModule.for_feature(FeatureFlags)], exports=[FeatureFlags])
    class FlagsModule:
        pass

    @injectable()
    class UsesFlags:
        def __init__(self, flags: FeatureFlags) -> None:
            self.flags = flags

    @module(imports=[FlagsModule], providers=[UsesFlags])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    assert asyncio.run(app.container.resolve(UsesFlags, owning_module=AppModule)).flags == FeatureFlags()


def test_root_with_is_global_false_is_scoped() -> None:
    root = ConfigModule.for_root(FeatureFlags, is_global=False, environ={"BETA": "1"})

    @injectable()
    class Consumer:
        def __init__(self, flags: FeatureFlags) -> None:
            self.flags = flags

    @module(providers=[Consumer], exports=[Consumer])
    class Elsewhere:
        pass

    @module(imports=[root, Elsewhere])
    class AppModule:
        pass

    with pytest.raises(MissingProviderError):
        LaurenFactory.create(AppModule)


def test_same_feature_settings_in_two_modules_is_rejected() -> None:
    @module(imports=[ConfigModule.for_feature(FeatureFlags)])
    class A:
        pass

    @module(imports=[ConfigModule.for_feature(FeatureFlags)])
    class B:
        pass

    @module(imports=[A, B])
    class AppModule:
        pass

    with pytest.raises(ModuleExportViolation):
        LaurenFactory.create(AppModule)


def test_global_settings_also_registered_elsewhere_is_rejected() -> None:
    settings = load_settings(FeatureFlags, environ={})

    @module(imports=[ConfigModule.for_root(FeatureFlags, environ={})])
    class AppModule:
        pass

    with pytest.raises(DuplicateBindingError) as excinfo:
        LaurenFactory.create(AppModule, global_providers=[use_value(provide=FeatureFlags, value=settings)])

    assert excinfo.value.detail["token"] == "FeatureFlags"


# ---------------------------------------------------------------------------
# expose_sections
# ---------------------------------------------------------------------------


def test_expose_sections_registers_nested_sections_globally() -> None:
    @module(
        imports=[
            ConfigModule.for_root(AppSettings, env_prefix="APP_", environ=GOOD_ENV, expose_sections=True)
        ]
    )
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    assert asyncio.run(app.container.resolve(DatabaseSettings)) == DatabaseSettings(url="sqlite://")


def test_exposed_section_of_an_unset_optional_section_is_none() -> None:
    @dataclass(frozen=True)
    class WithOptional:
        database: DatabaseSettings | None = None

    @module(imports=[ConfigModule.for_root(WithOptional, environ={}, expose_sections=True)])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    assert asyncio.run(app.container.resolve(DatabaseSettings)) is None


def test_expose_sections_on_a_feature_exports_the_sections() -> None:
    @injectable()
    class Repo:
        def __init__(self, db: DatabaseSettings) -> None:
            self.db = db

    @module(
        imports=[ConfigModule.for_feature(AppSettings, env_prefix="APP_", expose_sections=True)],
        providers=[Repo],
    )
    class FeatureModule:
        pass

    @module(imports=[ConfigModule.for_root(FeatureFlags, environ=GOOD_ENV), FeatureModule])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    assert asyncio.run(app.container.resolve(Repo, owning_module=FeatureModule)).db.url == "sqlite://"


def test_expose_sections_rejects_unresolvable_sections_at_declaration() -> None:
    @dataclass(frozen=True)
    class Broken:
        section: NotYetDefined  # type: ignore[name-defined]  # noqa: F821

    with pytest.raises(DecoratorUsageError) as excinfo:
        ConfigModule.for_feature(Broken, expose_sections=True)

    assert excinfo.value.detail["reason"] == "unresolvable_sections"


# ---------------------------------------------------------------------------
# Sources, inheritance and multi-app isolation
# ---------------------------------------------------------------------------


def test_feature_inherits_root_sources_and_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "settings.toml").write_text('[mail]\nhost = "toml-smtp"\nport = 2525\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    @module(
        imports=[ConfigModule.for_feature(MailSettings, env_prefix="MAIL_", section="mail")],
        providers=[Database],
    )
    class TomlMailModule:
        pass

    @module(
        imports=[
            ConfigModule.for_root(
                FeatureFlags, environ={"MAIL_PORT": "587"}, sources=[TomlSource("settings.toml"), EnvSource()]
            ),
            TomlMailModule,
        ]
    )
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    db = asyncio.run(app.container.resolve(Database, owning_module=TomlMailModule))
    assert db.settings == MailSettings(host="toml-smtp", port=587)


def test_feature_without_a_root_uses_the_default_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("MAIL_HOST=dotenv-smtp\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MAIL_HOST", raising=False)
    monkeypatch.delenv("LAUREN_PROFILE", raising=False)

    @module(imports=[MailModule])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    db = asyncio.run(app.container.resolve(Database, owning_module=MailModule))
    assert db.settings.host == "dotenv-smtp"
    assert asyncio.run(app.container.resolve(Profile)).is_development


def test_two_apps_with_different_environ_coexist() -> None:
    first = LaurenFactory.create(_app_module({**GOOD_ENV, "APP_DATABASE__POOL_SIZE": "1"}))
    second = LaurenFactory.create(_app_module({**GOOD_ENV, "APP_DATABASE__POOL_SIZE": "2"}))

    assert TestClient(first).get("/info/").json()["pool"] == 1
    assert TestClient(second).get("/info/").json()["pool"] == 2


def test_building_an_app_with_dotenv_never_touches_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "APP_SESSION_SECRET=x\nAPP_DATABASE__URL=y\nMAIL_HOST=z\nF12_DOTENV_ONLY=1\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    before = dict(os.environ)

    LaurenFactory.create(_app_module({}, sources=[DotenvSource(".env"), EnvSource()]))

    assert dict(os.environ) == before


def test_roots_must_agree_on_app_wide_options() -> None:
    @module(
        imports=[
            ConfigModule.for_root(FeatureFlags, environ={"A": "1"}),
            ConfigModule.for_root(MailSettings, environ={}),
        ]
    )
    class AppModule:
        pass

    with pytest.raises(StartupError) as excinfo:
        LaurenFactory.create(AppModule)

    assert excinfo.value.detail["reason"] == "conflicting_config_roots"


def test_agreeing_roots_are_both_global() -> None:
    environ = {"HOST": "smtp"}

    @module(
        imports=[
            ConfigModule.for_root(FeatureFlags, environ=environ),
            ConfigModule.for_root(MailSettings, environ=environ),
        ]
    )
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    assert asyncio.run(app.container.resolve(MailSettings)).host == "smtp"


def test_user_provided_profile_is_not_replaced() -> None:
    @module(imports=[ConfigModule.for_root(FeatureFlags, environ={})])
    class AppModule:
        pass

    app = LaurenFactory.create(
        AppModule, global_providers=[use_value(provide=Profile, value=Profile("custom"))]
    )

    assert asyncio.run(app.container.resolve(Profile)).name == "custom"


def test_lauren_imperative_api_includes_config_module() -> None:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_module(_app_module(GOOD_ENV))

    assert TestClient(app).get("/info/").json()["debug"] is False


# ---------------------------------------------------------------------------
# load_settings for factory kwargs
# ---------------------------------------------------------------------------


def test_load_settings_feeds_factory_kwargs_and_sessions_accept_secret() -> None:
    settings = load_settings(AppSettings, env_prefix="APP_", environ=GOOD_ENV)

    @controller("/s")
    class SessionController:
        @get("/")
        async def visit(self, session: Session) -> dict[str, int]:
            session["n"] = session.get("n", 0) + 1
            return {"n": session["n"]}

    @module(controllers=[SessionController])
    class AppModule:
        pass

    app = LaurenFactory.create(
        AppModule,
        sessions=SessionConfig(secret=settings.session_secret, secure=False),
        global_providers=[use_value(provide=AppSettings, value=settings)],
    )

    response = TestClient(app).get("/s/")
    assert response.status_code == 200
    assert "lauren_session=" in (response.header("set-cookie") or "")
    assert asyncio.run(app.container.resolve(AppSettings)) is settings


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def test_startup_log_names_profile_classes_and_sources_but_no_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("MAIL_HOST=smtp\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    logger = InMemoryLogger()

    LaurenFactory.create(_app_module(GOOD_ENV, sources=[DotenvSource(".env"), EnvSource()]), logger=logger)

    (line,) = [m for m in logger.messages() if m.startswith("profile=")]
    assert (
        line
        == "profile=development settings=AppSettings(4 keys) MailSettings(2 keys) sources=.env, environment"
    )
    assert "s3cr3t" not in " ".join(logger.messages())


def test_mutable_settings_class_logs_a_warning() -> None:
    logger = InMemoryLogger()

    @module(imports=[ConfigModule.for_root(MutableSettings, environ={})])
    class AppModule:
        pass

    LaurenFactory.create(AppModule, logger=logger)

    assert any("MutableSettings is not frozen" in m for m in logger.messages(level=LogLevel.WARN))


# ---------------------------------------------------------------------------
# Async sources (resolved in startup())
# ---------------------------------------------------------------------------


class Vault:
    name = "vault"

    def __init__(self, values: dict[str, str] | Exception) -> None:
        self.values = values
        self.calls = 0

    async def load(self, keys: Any) -> dict[str, str]:
        self.calls += 1
        if isinstance(self.values, Exception):
            raise self.values
        return {k: v for k, v in self.values.items() if k in keys}


def _async_app(vault: Vault, environ: dict[str, str], **kwargs: Any) -> type:
    @module(
        imports=[
            ConfigModule.for_root(
                AppSettings, env_prefix="APP_", environ=environ, sources=[vault, EnvSource()], **kwargs
            )
        ],
        controllers=[InfoController],
        providers=[Mailer],
    )
    class AppModule:
        pass

    return AppModule


def test_async_sourced_settings_load_at_startup() -> None:
    vault = Vault({"APP_SESSION_SECRET": "from-vault", "APP_DATABASE__URL": "vault://"})
    app = LaurenFactory.create(_async_app(vault, {"APP_DATABASE__POOL_SIZE": "4"}))

    assert vault.calls == 0
    with pytest.raises(StartupError) as excinfo:
        asyncio.run(app.container.resolve(AppSettings))
    assert excinfo.value.detail["reason"] == "settings_not_loaded"

    response = TestClient(app).get("/info/")

    assert vault.calls == 1
    assert response.json()["pool"] == 4


def test_errors_not_fixable_by_async_sources_still_fail_create() -> None:
    vault = Vault({})

    with pytest.raises(ConfigValidationError) as excinfo:
        LaurenFactory.create(_async_app(vault, {"APP_DATABASE__POOL_SIZE": "lots"}))

    assert [e["field"] for e in excinfo.value.detail["errors"]] == ["database.pool_size"]
    assert vault.calls == 0


def test_async_validation_failure_fails_startup() -> None:
    app = LaurenFactory.create(_async_app(Vault({"APP_SESSION_SECRET": "x"}), {}))

    with pytest.raises(ConfigValidationError) as excinfo:
        asyncio.run(app.startup())

    assert [(e["field"], e["reason"]) for e in excinfo.value.detail["errors"]] == [
        ("database.url", "missing")
    ]


def test_async_source_exception_fails_startup_with_provenance() -> None:
    app = LaurenFactory.create(
        _async_app(Vault(RuntimeError("sealed")), {"APP_SESSION_SECRET": "x", "APP_DATABASE__URL": "u"})
    )

    with pytest.raises(ConfigValidationError) as excinfo:
        asyncio.run(app.startup())

    (entry,) = excinfo.value.detail["errors"]
    assert entry["reason"] == "source_error" and entry["source"] == "vault"


def test_failed_async_startup_can_be_retried() -> None:
    vault = Vault(RuntimeError("sealed"))
    app = LaurenFactory.create(_async_app(vault, {"APP_SESSION_SECRET": "x"}))
    with pytest.raises(ConfigValidationError):
        asyncio.run(app.startup())

    vault.values = {"APP_DATABASE__URL": "vault://"}
    asyncio.run(app.startup())

    assert vault.calls == 2
    assert asyncio.run(app.container.resolve(AppSettings)).database.url == "vault://"


def test_async_global_exposed_sections_are_seeded_at_startup() -> None:
    vault = Vault({"APP_SESSION_SECRET": "x", "APP_DATABASE__URL": "vault://"})
    app = LaurenFactory.create(_async_app(vault, {}, expose_sections=True))

    asyncio.run(app.startup())

    assert asyncio.run(app.container.resolve(DatabaseSettings)).url == "vault://"


def test_async_feature_settings_are_seeded_at_startup() -> None:
    vault = Vault({"MAIL_HOST": "vault-smtp"})

    @module(
        imports=[ConfigModule.for_feature(MailSettings, env_prefix="MAIL_", sources=[vault, EnvSource()])],
        providers=[Database],
    )
    class AsyncMailModule:
        pass

    @module(imports=[ConfigModule.for_root(FeatureFlags, environ={}), AsyncMailModule])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)
    asyncio.run(app.startup())

    assert (
        asyncio.run(app.container.resolve(Database, owning_module=AsyncMailModule)).settings.host
        == "vault-smtp"
    )


def test_async_resolution_is_logged() -> None:
    logger = InMemoryLogger()
    app = LaurenFactory.create(
        _async_app(Vault({"APP_SESSION_SECRET": "x", "APP_DATABASE__URL": "u"}), {}), logger=logger
    )

    asyncio.run(app.startup())

    assert "async configuration loaded: AppSettings" in logger.messages()
    assert any("sources=environment, vault (async)" in m for m in logger.messages())


# ---------------------------------------------------------------------------
# Argument validation on the module factories
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("factory", "kwargs", "reason"),
    [
        (ConfigModule.for_root, {"sources": [EnvSource(), DotenvSource(".env")]}, "env_source_not_last"),
        (ConfigModule.for_feature, {"env_prefix": None}, "invalid_env_prefix"),
        (ConfigModule.for_root, {"environ": "APP_DEBUG=1"}, "invalid_environ"),
    ],
)
def test_module_factories_validate_arguments_eagerly(
    factory: Any, kwargs: dict[str, Any], reason: str
) -> None:
    with pytest.raises(DecoratorUsageError) as excinfo:
        factory(AppSettings, **kwargs)

    assert excinfo.value.detail["reason"] == reason


def test_each_call_returns_a_new_module_class() -> None:
    first = ConfigModule.for_root(FeatureFlags)
    second = ConfigModule.for_root(FeatureFlags)

    assert first is not second
    assert first.__name__ == "ConfigModule[FeatureFlags]"
