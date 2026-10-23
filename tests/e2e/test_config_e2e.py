"""End-to-end: typed configuration across every settings backend, with no secret leaking.

One app per backend mix: a dataclass root, a pydantic ``BaseModel`` feature
(skipped without pydantic) and a ``msgspec.Struct`` feature (skipped without
msgspec). Handlers return non-secret values plus the secret objects
themselves; neither the response bodies nor any log record may contain a
secret value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from lauren import LaurenFactory, controller, get, injectable, module
from lauren.config import ConfigModule, EnvSource, Secret
from lauren.exceptions import ConfigValidationError
from lauren.logging import InMemoryLogger
from lauren.testing import TestClient

SECRETS = ("root-secret-value", "pydantic-secret-value", "msgspec-secret-value")


@dataclass(frozen=True)
class CacheSection:
    host: str = "localhost"
    ttl_seconds: int = 60


@dataclass(frozen=True)
class RootSettings:
    api_key: Secret[str]
    cache: CacheSection
    debug: bool = False


try:
    import pydantic
except ImportError:  # pragma: no cover - exercised when pydantic is absent
    pydantic = None

try:
    import msgspec
except ImportError:  # pragma: no cover - exercised when msgspec is absent
    msgspec = None

if pydantic is not None:

    class BillingSettings(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(frozen=True)
        token: Secret[str]
        currency: str = "EUR"
        retries: int = pydantic.Field(default=3, ge=0)


if msgspec is not None:

    class SearchSettings(msgspec.Struct, frozen=True):
        password: Secret[str]
        index: str = "main"
        shards: int = 1


def _assert_no_secret(texts: list[str]) -> None:
    joined = "\n".join(texts)
    for value in SECRETS:
        assert value not in joined


def _log_texts(logger: InMemoryLogger) -> list[str]:
    return [r.message + json.dumps(r.extra, default=str) for r in logger.records]


@controller("/root")
class RootController:
    def __init__(self, settings: RootSettings) -> None:
        self.settings = settings

    @get("/")
    async def show(self) -> dict[str, Any]:
        return {"debug": self.settings.debug, "cache": self.settings.cache, "key": self.settings.api_key}


ROOT_ENV = {"APP_API_KEY": SECRETS[0], "APP_CACHE__TTL_SECONDS": "120", "APP_DEBUG": "yes"}


def _root(**extra: str) -> type:
    environ = {**ROOT_ENV, **extra}
    return ConfigModule.for_root(RootSettings, env_prefix="APP_", environ=environ, sources=[EnvSource()])


def test_dataclass_root_round_trip_without_leaks() -> None:
    logger = InMemoryLogger()

    @module(imports=[_root()], controllers=[RootController])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule, logger=logger)
    response = TestClient(app).get("/root/")

    assert response.json() == {
        "debug": True,
        "cache": {"host": "localhost", "ttl_seconds": 120},
        "key": "**********",
    }
    _assert_no_secret([response.text, *_log_texts(logger)])


@pytest.mark.pydantic
def test_pydantic_feature_section_without_leaks() -> None:
    pytest.importorskip("pydantic")
    logger = InMemoryLogger()

    @injectable()
    class Billing:
        def __init__(self, settings: BillingSettings) -> None:
            self.settings = settings

    @controller("/billing")
    class BillingController:
        def __init__(self, billing: Billing) -> None:
            self.billing = billing

        @get("/")
        async def show(self) -> Any:
            return self.billing.settings

    @module(
        imports=[ConfigModule.for_feature(BillingSettings, env_prefix="BILLING_")],
        providers=[Billing],
        controllers=[BillingController],
    )
    class BillingModule:
        pass

    @module(
        imports=[_root(BILLING_TOKEN=SECRETS[1], BILLING_RETRIES="5"), BillingModule],
        controllers=[RootController],
    )
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule, logger=logger)
    client = TestClient(app)
    billing = client.get("/billing/")
    root = client.get("/root/")

    assert billing.json() == {"token": "**********", "currency": "EUR", "retries": 5}
    _assert_no_secret([billing.text, root.text, *_log_texts(logger)])


@pytest.mark.msgspec
def test_msgspec_feature_section_without_leaks() -> None:
    pytest.importorskip("msgspec")
    logger = InMemoryLogger()

    @controller("/search")
    class SearchController:
        def __init__(self, settings: SearchSettings) -> None:
            self.settings = settings

        @get("/")
        async def show(self) -> dict[str, Any]:
            return {
                "index": self.settings.index,
                "shards": self.settings.shards,
                "password": self.settings.password,
            }

    @module(
        imports=[ConfigModule.for_feature(SearchSettings, env_prefix="SEARCH_")],
        controllers=[SearchController],
    )
    class SearchModule:
        pass

    @module(imports=[_root(SEARCH_PASSWORD=SECRETS[2], SEARCH_SHARDS="3"), SearchModule])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule, logger=logger)
    response = TestClient(app).get("/search/")

    assert response.json() == {"index": "main", "shards": 3, "password": "**********"}
    _assert_no_secret([response.text, *_log_texts(logger)])


def test_invalid_secret_value_is_not_echoed_by_the_startup_error() -> None:
    @dataclass(frozen=True)
    class PinSettings:
        pin: Secret[int]

    @module(imports=[ConfigModule.for_root(PinSettings, environ={"PIN": SECRETS[0]}, sources=[EnvSource()])])
    class AppModule:
        pass

    with pytest.raises(ConfigValidationError) as excinfo:
        LaurenFactory.create(AppModule)

    _assert_no_secret([str(excinfo.value), json.dumps(excinfo.value.detail)])
