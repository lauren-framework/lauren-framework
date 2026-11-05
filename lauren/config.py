"""Typed configuration: settings classes validated inside ``LaurenFactory.create``.

Declare settings as an ordinary class — a frozen dataclass needs no extra
dependency; pydantic models, pydantic-settings ``BaseSettings`` and
``msgspec.Struct`` work too::

    from dataclasses import dataclass

    from lauren import injectable, module
    from lauren.config import ConfigModule, Secret


    @dataclass(frozen=True)
    class DatabaseSettings:
        url: str
        pool_size: int = 10


    @dataclass(frozen=True)
    class AppSettings:
        session_secret: Secret[str]
        database: DatabaseSettings
        debug: bool = False


    @module(imports=[ConfigModule.for_root(AppSettings, env_prefix="APP_")])
    class AppModule:
        pass


    @injectable()
    class Mailer:
        def __init__(self, settings: AppSettings) -> None:
            self.debug = settings.debug

``APP_SESSION_SECRET``, ``APP_DATABASE__URL`` and friends are read from
``.env``, ``.env.{profile}`` and the environment (the environment always
wins; ``os.environ`` is never written). A missing or invalid value
anywhere in the app makes ``LaurenFactory.create`` raise one
:class:`~lauren.exceptions.ConfigValidationError` listing every problem
with its key and source.
"""

from __future__ import annotations

from ._config._loader import Env, Profile, aload_settings, load_settings
from ._config._module import ConfigModule
from ._config._secret import Secret
from ._config._sources import (
    AsyncSource,
    DotenvSource,
    EnvSource,
    SecretsDirSource,
    TomlSource,
    YamlSource,
)
from .exceptions import ConfigValidationError

__all__ = [
    "AsyncSource",
    "ConfigModule",
    "ConfigValidationError",
    "DotenvSource",
    "Env",
    "EnvSource",
    "Profile",
    "Secret",
    "SecretsDirSource",
    "TomlSource",
    "YamlSource",
    "aload_settings",
    "load_settings",
]
