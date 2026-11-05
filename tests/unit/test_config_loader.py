"""Unit tests for the settings loader (``load_settings`` / ``aload_settings``)."""

from __future__ import annotations

import asyncio
import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

import pytest

from lauren.config import (
    DotenvSource,
    Env,
    EnvSource,
    Profile,
    Secret,
    SecretsDirSource,
    TomlSource,
    aload_settings,
    load_settings,
)
from lauren.exceptions import ConfigValidationError, DecoratorUsageError, DuplicateBindingError, StartupError
from lauren._config._loader import build_node, exposed_section_types, format_entry

# ---------------------------------------------------------------------------
# Settings classes (module level so PEP 563 annotations resolve)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseSettings:
    url: str
    pool_size: int = 10
    echo: bool = False


@dataclass(frozen=True)
class AppSettings:
    session_secret: Secret[str]
    database: DatabaseSettings
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = "INFO"
    allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:3000"])
    llm_api_key: Annotated[Secret[str], Env("LLM_API_KEY", "OPENAI_API_KEY")] = Secret("")


@dataclass(frozen=True)
class CacheSettings:
    host: str = "localhost"
    port: int = 6379


@dataclass(frozen=True)
class WithOptionalSection:
    name: str = "app"
    cache: CacheSettings | None = None


@dataclass(frozen=True)
class WithDefaultSection:
    database: DatabaseSettings = field(default_factory=lambda: DatabaseSettings(url="sqlite://"))


@dataclass(frozen=True)
class WithDefaultedRequiredSection:
    cache: CacheSettings


@dataclass(frozen=True)
class Port:
    value: int

    def __post_init__(self) -> None:
        if not 0 < self.value < 65536:
            raise ValueError("port out of range")


@dataclass(frozen=True)
class Strange:
    handler: Any = None
    callback: type = int


@dataclass(frozen=True)
class Unsupported:
    thing: complex = 0j


@dataclass(frozen=True)
class Node:
    child: Node | None = None


@dataclass(frozen=True)
class Replicated:
    primary: DatabaseSettings
    replica: DatabaseSettings


@dataclass(frozen=True)
class Plain:
    value: str


class TD(TypedDict):
    value: str


ENV = {"APP_SESSION_SECRET": "s3cr3t", "APP_DATABASE__URL": "postgres://db"}


# ---------------------------------------------------------------------------
# Binding and naming
# ---------------------------------------------------------------------------


def test_load_settings_binds_prefixed_and_nested_names() -> None:
    settings = load_settings(AppSettings, env_prefix="APP_", environ={**ENV, "APP_DATABASE__POOL_SIZE": "5"})

    assert settings.session_secret.get_secret_value() == "s3cr3t"
    assert settings.database == DatabaseSettings(url="postgres://db", pool_size=5)
    assert settings.debug is False
    assert settings.allowed_origins == ["http://localhost:3000"]


def test_lowercase_prefix_is_upper_cased() -> None:
    settings = load_settings(DatabaseSettings, env_prefix="db_", environ={"DB_URL": "x"})

    assert settings.url == "x"


def test_explicit_env_names_ignore_the_prefix_and_take_the_first_present() -> None:
    first = load_settings(AppSettings, env_prefix="APP_", environ={**ENV, "OPENAI_API_KEY": "b"})
    both = load_settings(
        AppSettings, env_prefix="APP_", environ={**ENV, "LLM_API_KEY": "a", "OPENAI_API_KEY": "b"}
    )

    assert first.llm_api_key.get_secret_value() == "b"
    assert both.llm_api_key.get_secret_value() == "a"


def test_collections_and_literals_are_coerced() -> None:
    settings = load_settings(
        AppSettings,
        env_prefix="APP_",
        environ={
            **ENV,
            "APP_ALLOWED_ORIGINS": "https://a, https://b",
            "APP_LOG_LEVEL": "WARN",
            "APP_DEBUG": "on",
        },
    )

    assert settings.allowed_origins == ["https://a", "https://b"]
    assert settings.log_level == "WARN"
    assert settings.debug is True


def test_empty_sources_use_class_defaults_only() -> None:
    assert load_settings(CacheSettings, sources=[]) == CacheSettings()


def test_os_environ_is_read_when_no_mapping_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("F12PROBE_HOST", "redis")

    settings = load_settings(CacheSettings, env_prefix="F12PROBE_", sources=[EnvSource()])

    assert settings.host == "redis"


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("layers", "expected"),
    [
        ({}, "default"),
        ({"toml"}, "toml"),
        ({"toml", "env_file"}, "env_file"),
        ({"toml", "env_file", "profile_file"}, "profile_file"),
        ({"toml", "env_file", "profile_file", "environ"}, "environ"),
        ({"toml", "env_file", "profile_file", "environ", "override"}, "override"),
        ({"toml", "override"}, "override"),
        ({"environ"}, "environ"),
    ],
)
def test_source_precedence(tmp_path: Path, layers: set[str], expected: str) -> None:
    @dataclass(frozen=True)
    class Named:
        name: str = "default"

    if "toml" in layers:
        _write(tmp_path / "settings.toml", 'name = "toml"\n')
    if "env_file" in layers:
        _write(tmp_path / ".env", "NAME=env_file\n")
    if "profile_file" in layers:
        _write(tmp_path / ".env.test", "NAME=profile_file\n")
    environ = {"NAME": "environ"} if "environ" in layers else {}
    overrides = {"name": "override"} if "override" in layers else None

    settings = load_settings(
        Named,
        profile="test",
        environ=environ,
        overrides=overrides,
        sources=[
            TomlSource(tmp_path / "settings.toml", required=False),
            DotenvSource(tmp_path / ".env", required=False),
            DotenvSource(tmp_path / ".env.{profile}", required=False),
            EnvSource(),
        ],
    )

    assert settings.name == expected


def test_dotenv_never_overrides_the_environment(tmp_path: Path) -> None:
    env_file = _write(tmp_path / ".env", "APP_DEBUG=true\n")

    settings = load_settings(
        AppSettings,
        env_prefix="APP_",
        environ={**ENV, "APP_DEBUG": "false"},
        sources=[DotenvSource(env_file), EnvSource()],
    )

    assert settings.debug is False


def test_loading_never_writes_os_environ(tmp_path: Path) -> None:
    env_file = _write(tmp_path / ".env", "APP_SESSION_SECRET=x\nAPP_DATABASE__URL=y\nF12_ONLY_IN_FILE=1\n")
    before = dict(os.environ)

    load_settings(AppSettings, env_prefix="APP_", environ={}, sources=[DotenvSource(env_file), EnvSource()])

    assert dict(os.environ) == before


def test_toml_tables_map_to_sections_and_values_are_strict(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.toml", 'session_secret = "x"\n[database]\nurl = "u"\npool_size = "10"\n')

    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(AppSettings, sources=[TomlSource(path)])

    (entry,) = excinfo.value.detail["errors"]
    assert entry["field"] == "database.pool_size"
    assert entry["key"] == "database.pool_size"
    assert entry["source"] == str(path)
    assert entry["value"] == "10"


def test_section_option_selects_a_toml_table(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.toml", '[services.db]\nurl = "toml://"\npool_size = 3\n')

    settings = load_settings(DatabaseSettings, section="services.db", sources=[TomlSource(path)])

    assert settings == DatabaseSettings(url="toml://", pool_size=3)


def test_secrets_dir_source_supplies_values(tmp_path: Path) -> None:
    _write(tmp_path / "APP_SESSION_SECRET", "from-file\n")

    settings = load_settings(
        AppSettings,
        env_prefix="APP_",
        environ={"APP_DATABASE__URL": "u"},
        sources=[SecretsDirSource(tmp_path), EnvSource()],
    )

    assert settings.session_secret.get_secret_value() == "from-file"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def test_unset_optional_section_keeps_its_default() -> None:
    assert load_settings(WithOptionalSection, environ={}).cache is None


def test_partially_set_optional_section_is_built() -> None:
    settings = load_settings(WithOptionalSection, environ={"CACHE__PORT": "1"})

    assert settings.cache == CacheSettings(port=1)


def test_unset_section_with_default_factory_keeps_the_default() -> None:
    assert load_settings(WithDefaultSection, environ={}).database.url == "sqlite://"


def test_optional_section_reports_missing_leaves_once_any_leaf_is_set() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(WithDefaultSection, environ={"DATABASE__POOL_SIZE": "3"})

    assert [e["field"] for e in excinfo.value.detail["errors"]] == ["database.url"]


def test_required_section_whose_fields_all_have_defaults_is_built() -> None:
    assert load_settings(WithDefaultedRequiredSection, environ={}).cache == CacheSettings()


def test_scalar_for_a_section_is_invalid(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.toml", 'cache = "redis://"\n')

    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(WithDefaultedRequiredSection, sources=[TomlSource(path)])

    (entry,) = excinfo.value.detail["errors"]
    assert entry["field"] == "cache"
    assert "expected a table" in entry["message"]


def test_override_may_supply_a_whole_section_instance() -> None:
    settings = load_settings(
        AppSettings,
        env_prefix="APP_",
        environ={"APP_SESSION_SECRET": "x"},
        overrides={"database": DatabaseSettings("o")},
    )

    assert settings.database.url == "o"


def test_override_none_for_an_optional_section() -> None:
    settings = load_settings(WithOptionalSection, environ={"CACHE__PORT": "1"}, overrides={"cache": None})

    assert settings.cache is None


def test_override_mapping_for_a_section_is_merged_leaf_by_leaf() -> None:
    settings = load_settings(
        AppSettings,
        env_prefix="APP_",
        environ={"APP_SESSION_SECRET": "x"},
        overrides={"database": {"url": "o"}, "database.pool_size": "4"},
    )

    assert settings.database == DatabaseSettings(url="o", pool_size=4)


def test_string_overrides_are_coerced_leniently() -> None:
    settings = load_settings(
        AppSettings, env_prefix="APP_", environ=ENV, overrides={"debug": "yes", "database.echo": True}
    )

    assert settings.debug is True and settings.database.echo is True


def test_merging_overrides_never_mutates_the_callers_mapping() -> None:
    section = {"url": "o"}

    load_settings(
        AppSettings,
        env_prefix="APP_",
        environ={"APP_SESSION_SECRET": "x"},
        overrides={"database": section, "database.pool_size": "4"},
    )

    assert section == {"url": "o"}


def test_unknown_override_fields_are_errors() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(
            AppSettings, env_prefix="APP_", environ=ENV, overrides={"databse.url": "x", "database.nope": 1}
        )

    fields = [(e["field"], e["reason"]) for e in excinfo.value.detail["errors"]]
    assert fields == [("databse", "unknown_field"), ("database.nope", "unknown_field")]


# ---------------------------------------------------------------------------
# Aggregation and provenance
# ---------------------------------------------------------------------------


def test_every_problem_is_reported_at_once_with_provenance() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(
            AppSettings,
            env_prefix="APP_",
            profile="production",
            environ={"APP_DATABASE__POOL_SIZE": "ten", "APP_DEBUG": "maybe"},
        )

    detail = excinfo.value.detail
    assert detail["profile"] == "production"
    assert [(e["field"], e["reason"]) for e in detail["errors"]] == [
        ("session_secret", "missing"),
        ("database.url", "missing"),
        ("database.pool_size", "invalid"),
        ("debug", "invalid"),
    ]
    missing = detail["errors"][0]
    assert missing["key"] == "APP_SESSION_SECRET"
    assert missing["searched"] == [".env", ".env.production", "environment"]
    invalid = detail["errors"][2]
    assert invalid == {
        "settings": "AppSettings",
        "field": "database.pool_size",
        "reason": "invalid",
        "key": "APP_DATABASE__POOL_SIZE",
        "expected": "int",
        "source": "environment",
        "value": "ten",
        "message": "invalid int 'ten'",
    }
    assert "4 configuration error(s) in profile 'production'" in str(excinfo.value)


def test_secret_values_are_never_echoed_in_errors() -> None:
    @dataclass(frozen=True)
    class Keys:
        port: Secret[int]

    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(Keys, environ={"PORT": "hunter2"})

    (entry,) = excinfo.value.detail["errors"]
    assert "value" not in entry
    assert "hunter2" not in str(excinfo.value)
    assert "hunter2" not in repr(excinfo.value.detail)


def test_secrets_nested_in_collections_are_never_echoed() -> None:
    @dataclass(frozen=True)
    class Pins:
        pins: list[Secret[int]]

    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(Pins, environ={"PINS": "1234,hunter2"})

    (entry,) = excinfo.value.detail["errors"]
    assert "value" not in entry
    assert "hunter2" not in str(excinfo.value)


def test_source_problems_are_aggregated_with_field_errors(tmp_path: Path) -> None:
    bad = _write(tmp_path / ".env", "oops\n")

    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(
            Plain, environ={}, sources=[DotenvSource(bad), TomlSource(tmp_path / "missing.toml"), EnvSource()]
        )

    reasons = [e["reason"] for e in excinfo.value.detail["errors"]]
    assert reasons == ["parse_error", "missing_file", "missing"]
    assert excinfo.value.detail["errors"][0]["line"] == 1
    assert f"{bad}: line 1:" in str(excinfo.value)


def test_post_init_errors_are_reported_against_the_class() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(Port, environ={"VALUE": "70000"})

    (entry,) = excinfo.value.detail["errors"]
    assert entry["settings"] == "Port"
    assert entry["field"] == ""
    assert "port out of range" in entry["message"]
    assert format_entry(entry).startswith("Port  ")


def test_unsupported_field_type_is_reported() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(Unsupported, environ={"THING": "1+2j"})

    (entry,) = excinfo.value.detail["errors"]
    assert entry["expected"] == "complex"
    assert "unsupported type" in entry["message"]


def test_any_typed_field_receives_the_raw_string() -> None:
    assert load_settings(Strange, environ={"HANDLER": "x"}).handler == "x"


def test_union_with_several_members_tries_each_in_order() -> None:
    @dataclass(frozen=True)
    class Flexible:
        limit: int | str | None = None

    assert load_settings(Flexible, environ={"LIMIT": "10"}).limit == 10
    assert load_settings(Flexible, environ={"LIMIT": "unbounded"}).limit == "unbounded"


def test_invalid_typed_file_values_are_reported_with_their_repr(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.toml", "value = [1, 2]\n")

    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(Plain, sources=[TomlSource(path)])

    (entry,) = excinfo.value.detail["errors"]
    assert entry["value"] == "[1, 2]"


def test_missing_with_no_sources_says_so() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(Plain, sources=[])

    assert excinfo.value.detail["errors"][0]["searched"] == ["(no sources)"]


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "environ", "expected"),
    [
        (None, {}, "development"),
        (None, {"LAUREN_PROFILE": "production"}, "production"),
        (None, {"LAUREN_PROFILE": "  "}, "development"),
        ("test", {"LAUREN_PROFILE": "production"}, "test"),
    ],
)
def test_profile_selection(
    tmp_path: Path, profile: str | None, environ: dict[str, str], expected: str
) -> None:
    _write(tmp_path / f".env.{expected}", f"VALUE={expected}\n")

    settings = load_settings(
        Plain,
        profile=profile,
        environ=environ,
        sources=[DotenvSource(tmp_path / ".env.{profile}"), EnvSource()],
    )

    assert settings.value == expected


@pytest.mark.parametrize(
    ("profile", "environ", "source"),
    [("staging", {}, "load_settings(profile=...)"), (None, {"LAUREN_PROFILE": "staging"}, "environment")],
)
def test_unknown_profile_is_a_config_error(profile: str | None, environ: dict[str, str], source: str) -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        load_settings(Plain, profile=profile, environ=environ)

    (entry,) = excinfo.value.detail["errors"]
    assert entry["reason"] == "unknown_profile"
    assert entry["profile"] == "staging"
    assert entry["allowed"] == ["development", "test", "production"]
    assert entry["source"] == source


def test_custom_profiles_are_accepted() -> None:
    settings = load_settings(Plain, profile="staging", profiles=("staging",), environ={"VALUE": "v"})

    assert settings.value == "v"


def test_profile_flags() -> None:
    assert Profile("development").is_development
    assert Profile("test").is_test
    assert Profile("production").is_production
    assert not Profile("staging").is_production


# ---------------------------------------------------------------------------
# Argument validation (code errors, raised eagerly)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"env_prefix": 1}, "invalid_env_prefix"),
        ({"sources": "EnvSource()"}, "invalid_sources"),
        ({"sources": [EnvSource]}, "invalid_sources"),
        ({"sources": [object()]}, "invalid_sources"),
        ({"sources": [EnvSource(), DotenvSource(".env")]}, "env_source_not_last"),
        ({"sources": [EnvSource(), EnvSource()]}, "env_source_not_last"),
        ({"section": ""}, "invalid_section"),
        ({"section": 3}, "invalid_section"),
        ({"profile": ""}, "invalid_profile"),
        ({"profiles": "production"}, "invalid_profiles"),
        ({"profiles": ()}, "invalid_profiles"),
        ({"profiles": ("",)}, "invalid_profiles"),
        ({"environ": [("A", "1")]}, "invalid_environ"),
        ({"overrides": [("a", 1)]}, "invalid_overrides"),
        ({"overrides": {"": 1}}, "invalid_overrides"),
        ({"overrides": {".": 1}}, "invalid_overrides"),
    ],
)
def test_invalid_arguments_raise_decorator_usage_error(kwargs: dict[str, Any], reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as excinfo:
        load_settings(Plain, **kwargs)

    assert excinfo.value.detail == {"helper": "load_settings", "reason": reason}


@pytest.mark.parametrize("settings_cls", [TD, dict, "AppSettings", object])
def test_unsupported_settings_classes_are_rejected(settings_cls: Any) -> None:
    with pytest.raises(DecoratorUsageError) as excinfo:
        load_settings(settings_cls)

    assert excinfo.value.detail["reason"] == "unsupported_settings_class"


def test_typeddict_rejection_suggests_a_dataclass() -> None:
    with pytest.raises(DecoratorUsageError, match="frozen"):
        load_settings(TD)


def test_load_settings_rejects_async_sources() -> None:
    class Vault:
        async def load(self, keys: Any) -> dict[str, str]:
            return {}

    with pytest.raises(DecoratorUsageError) as excinfo:
        load_settings(Plain, sources=[Vault()])

    assert excinfo.value.detail["reason"] == "async_source_in_sync_loader"


@pytest.mark.parametrize("names", [(), ("",), (1,)])
def test_env_marker_validates_names(names: tuple[Any, ...]) -> None:
    with pytest.raises(DecoratorUsageError):
        Env(*names)


def test_env_marker_value_semantics() -> None:
    assert Env("A", "B") == Env("A", "B")
    assert Env("A") != Env("B")
    assert hash(Env("A")) == hash(Env("A"))
    assert repr(Env("A", "B")) == "Env('A', 'B')"


def test_unresolvable_annotation_is_a_startup_error() -> None:
    @dataclass(frozen=True)
    class Broken:
        value: NotDefinedAnywhere  # type: ignore[name-defined]  # noqa: F821

    with pytest.raises(StartupError) as excinfo:
        load_settings(Broken, environ={})

    assert excinfo.value.detail["reason"] == "unresolvable_settings_annotation"


def test_recursive_settings_are_rejected() -> None:
    with pytest.raises(StartupError) as excinfo:
        load_settings(Node, environ={})

    assert excinfo.value.detail["reason"] == "recursive_settings"


def test_function_local_settings_classes_resolve() -> None:
    @dataclass(frozen=True)
    class LocalSection:
        value: int = 1

    @dataclass(frozen=True)
    class LocalSettings:
        section: LocalSection

    assert load_settings(LocalSettings, environ={"SECTION__VALUE": "2"}).section.value == 2


def test_exposed_section_types_reject_duplicates() -> None:
    node = build_node(Replicated, env_prefix="")

    with pytest.raises(DuplicateBindingError):
        exposed_section_types(node)


def test_non_frozen_dataclass_still_loads() -> None:
    @dataclass
    class Mutable:
        value: str = "x"

    assert dataclasses.asdict(load_settings(Mutable, environ={})) == {"value": "x"}


# ---------------------------------------------------------------------------
# Async loading
# ---------------------------------------------------------------------------


class _Vault:
    name = "vault"

    def __init__(self, values: dict[str, str] | Exception) -> None:
        self.values = values
        self.keys: list[str] = []

    async def load(self, keys: Any) -> dict[str, str]:
        self.keys = list(keys)
        if isinstance(self.values, Exception):
            raise self.values
        return self.values


def test_aload_settings_awaits_async_sources_below_the_environment() -> None:
    vault = _Vault({"APP_SESSION_SECRET": "vault", "APP_DATABASE__URL": "vault://"})

    settings = asyncio.run(
        aload_settings(
            AppSettings,
            env_prefix="APP_",
            environ={"APP_DATABASE__URL": "env://"},
            sources=[vault, EnvSource()],
        )
    )

    assert settings.session_secret.get_secret_value() == "vault"
    assert settings.database.url == "env://"
    assert "APP_SESSION_SECRET" in vault.keys and "LLM_API_KEY" in vault.keys


def test_aload_settings_aggregates_async_source_failures() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        asyncio.run(aload_settings(Plain, environ={}, sources=[_Vault(RuntimeError("sealed")), EnvSource()]))

    reasons = [(e["reason"], e.get("source")) for e in excinfo.value.detail["errors"]]
    assert reasons == [("source_error", "vault"), ("missing", None)]


# ---------------------------------------------------------------------------
# pydantic / msgspec / pydantic-settings backends
# ---------------------------------------------------------------------------


@pytest.mark.pydantic
class TestPydanticSettings:
    def _models(self) -> tuple[type, type]:
        pydantic = pytest.importorskip("pydantic")

        class Db(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(frozen=True)
            url: str
            pool: int = pydantic.Field(default=5, gt=0)

        class Settings(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(frozen=True)
            token: Secret[str]
            db: Db
            tags: list[str] = []
            site: pydantic.AnyUrl = pydantic.AnyUrl("https://example.com")
            renamed: str = pydantic.Field(default="d", alias="aliasName")

        return Settings, Db

    def test_model_is_built_from_coerced_values(self) -> None:
        settings_cls, db_cls = self._models()

        settings = load_settings(
            settings_cls,
            environ={
                "TOKEN": "t",
                "DB__URL": "u",
                "DB__POOL": "3",
                "TAGS": "a,b",
                "SITE": "https://lauren.dev",
                "RENAMED": "r",
            },
        )

        assert settings.db == db_cls(url="u", pool=3)
        assert settings.tags == ["a", "b"]
        assert str(settings.site) == "https://lauren.dev/"
        assert settings.renamed == "r"
        assert settings.token.get_secret_value() == "t"

    def test_missing_and_constraint_errors_arrive_together(self) -> None:
        settings_cls, _ = self._models()

        with pytest.raises(ConfigValidationError) as excinfo:
            load_settings(settings_cls, environ={"TOKEN": "t", "DB__POOL": "0"})

        errors = excinfo.value.detail["errors"]
        assert [(e["field"], e["reason"]) for e in errors] == [("db.url", "missing"), ("db.pool", "invalid")]
        assert errors[1]["source"] == "environment" and errors[1]["value"] == "0"
        assert errors[1]["expected"] == "greater_than"

    def test_model_validator_errors_are_reported_against_the_class(self) -> None:
        pydantic = pytest.importorskip("pydantic")

        class Window(pydantic.BaseModel):
            low: int
            high: int

            @pydantic.model_validator(mode="after")
            def ordered(self) -> Window:
                if self.low > self.high:
                    raise ValueError("low must not exceed high")
                return self

        with pytest.raises(ConfigValidationError) as excinfo:
            load_settings(Window, environ={"LOW": "5", "HIGH": "1"})

        (entry,) = excinfo.value.detail["errors"]
        assert entry["field"] == "" and "low must not exceed high" in entry["message"]

    def test_unsupported_types_are_validated_by_the_model(self) -> None:
        settings_cls, _ = self._models()

        with pytest.raises(ConfigValidationError) as excinfo:
            load_settings(settings_cls, environ={"TOKEN": "t", "DB__URL": "u", "SITE": "not a url"})

        (entry,) = excinfo.value.detail["errors"]
        assert entry["field"] == "site" and entry["value"] == "not a url"

    def test_secret_field_errors_hide_the_value(self) -> None:
        pydantic = pytest.importorskip("pydantic")

        class Keys(pydantic.BaseModel):
            pin: Secret[int] = pydantic.Field(default=Secret(1))
            token: pydantic.SecretStr = pydantic.SecretStr("x")

            @pydantic.field_validator("pin")
            @classmethod
            def short(cls, value: Secret[int]) -> Secret[int]:
                if value.get_secret_value() > 9999:
                    raise ValueError("pin too long")
                return value

        with pytest.raises(ConfigValidationError) as excinfo:
            load_settings(Keys, environ={"PIN": "123456", "TOKEN": "tok"})

        (entry,) = excinfo.value.detail["errors"]
        assert "value" not in entry and "123456" not in str(excinfo.value)

    def test_delegated_pydantic_settings_is_called_with_overrides(self) -> None:
        pydantic = pytest.importorskip("pydantic")

        class BaseSettings(pydantic.BaseModel):
            pass

        BaseSettings.__module__ = "pydantic_settings.main"

        class Delegated(BaseSettings):
            model_config = pydantic.ConfigDict(frozen=True)
            url: str

        assert load_settings(Delegated, overrides={"url": "x"}).url == "x"
        with pytest.raises(ConfigValidationError) as excinfo:
            load_settings(Delegated)
        (entry,) = excinfo.value.detail["errors"]
        assert entry["field"] == "url" and entry["reason"] == "missing"
        assert entry["searched"] == ["pydantic-settings"]

    def test_delegated_settings_errors_other_than_validation_propagate(self) -> None:
        pydantic = pytest.importorskip("pydantic")

        class BaseSettings(pydantic.BaseModel):
            pass

        BaseSettings.__module__ = "pydantic_settings.main"

        class Exploding(BaseSettings):
            def __init__(self, **data: Any) -> None:
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            load_settings(Exploding)

    def test_nested_pydantic_settings_section_is_validated_as_a_model(self) -> None:
        pydantic = pytest.importorskip("pydantic")

        class BaseSettings(pydantic.BaseModel):
            pass

        BaseSettings.__module__ = "pydantic_settings.main"

        class Inner(BaseSettings):
            url: str

        @dataclass(frozen=True)
        class Outer:
            inner: Inner

        assert load_settings(Outer, environ={"INNER__URL": "u"}).inner.url == "u"

    def test_delegated_pydantic_settings_rejects_lauren_source_options(self) -> None:
        pydantic = pytest.importorskip("pydantic")

        class BaseSettings(pydantic.BaseModel):
            pass

        BaseSettings.__module__ = "pydantic_settings.main"

        class Delegated(BaseSettings):
            url: str = "x"

        with pytest.raises(DecoratorUsageError) as excinfo:
            load_settings(Delegated, env_prefix="APP_")

        assert excinfo.value.detail["reason"] == "delegated_settings_options"


try:
    import msgspec
except ImportError:  # pragma: no cover - exercised when msgspec is absent
    msgspec = None

if msgspec is not None:

    class MsgDb(msgspec.Struct, frozen=True):
        url: str
        pool: int = 5

    class MsgSettings(msgspec.Struct, frozen=True):
        token: Secret[str]
        db: MsgDb
        debug: bool = False

    class MsgBounded(msgspec.Struct, frozen=True):
        size: Annotated[int, msgspec.Meta(gt=0)]

    class MsgUnresolvable(msgspec.Struct):
        value: NotDefinedAnywhere  # type: ignore[name-defined]  # noqa: F821


@pytest.mark.msgspec
class TestMsgspecSettings:
    def test_struct_is_built_from_coerced_values(self) -> None:
        settings = load_settings(MsgSettings, environ={"TOKEN": "t", "DB__URL": "u", "DEBUG": "on"})

        assert settings.db == MsgDb(url="u") and settings.debug is True
        assert settings.token.get_secret_value() == "t"

    def test_struct_constraint_errors_are_mapped_to_fields(self) -> None:
        with pytest.raises(ConfigValidationError) as excinfo:
            load_settings(MsgBounded, environ={"SIZE": "0"})

        (entry,) = excinfo.value.detail["errors"]
        assert entry["field"] == "size" and entry["source"] == "environment"
        assert entry["value"] == "0"

    def test_struct_missing_fields_are_reported(self) -> None:
        with pytest.raises(ConfigValidationError) as excinfo:
            load_settings(MsgSettings, environ={"DB__POOL": "x"})

        reasons = [(e["field"], e["reason"]) for e in excinfo.value.detail["errors"]]
        assert reasons == [("token", "missing"), ("db.url", "missing"), ("db.pool", "invalid")]

    def test_unresolvable_struct_annotation_is_a_startup_error(self) -> None:
        with pytest.raises(StartupError) as excinfo:
            load_settings(MsgUnresolvable, environ={})

        assert excinfo.value.detail["reason"] == "unresolvable_settings_annotation"
