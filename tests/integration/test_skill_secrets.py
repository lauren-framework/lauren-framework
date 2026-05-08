"""Integration tests for the secrets management pattern (Skill 18).

Uses InMemorySecretsProvider — no Vault or AWS credentials required.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

import pytest

from lauren import LaurenFactory, Path, Scope, controller, get, injectable, module
from lauren import use_value
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Abstract interface + implementations
# ---------------------------------------------------------------------------


class SecretsProvider(ABC):
    @abstractmethod
    def get_secret(self, name: str) -> str: ...


class InMemorySecretsProvider(SecretsProvider):
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets: dict[str, str] = secrets or {}

    def set_secret(self, name: str, value: str) -> None:
        self._secrets[name] = value

    def get_secret(self, name: str) -> str:
        if name not in self._secrets:
            raise KeyError(f"Secret '{name}' not found")
        return self._secrets[name]


class EnvSecretsProvider(SecretsProvider):
    def get_secret(self, name: str) -> str:
        val = os.getenv(name)
        if val is None:
            raise KeyError(f"Secret '{name}' not found in environment")
        return val


@injectable(scope=Scope.SINGLETON)
class SecretsService:
    def __init__(self, provider: SecretsProvider) -> None:
        self._provider = provider

    def get(self, name: str) -> str:
        return self._provider.get_secret(name)


# ---------------------------------------------------------------------------
# Controller + module
# ---------------------------------------------------------------------------


@controller("/secrets")
class SecretsDemoController:
    def __init__(self, secrets: SecretsService) -> None:
        self._secrets = secrets

    @get("/{name}")
    async def get_secret(self, name: Path[str]) -> dict:
        try:
            return {"name": name, "value": self._secrets.get(name)}
        except KeyError:
            return {"name": name, "value": None}


def build_app(provider: SecretsProvider):
    @module(
        controllers=[SecretsDemoController],
        providers=[
            use_value(provide=SecretsProvider, value=provider),
            SecretsService,
        ],
    )
    class TestModule:
        pass

    return TestClient(LaurenFactory.create(TestModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSecretsManagement:
    def test_get_secret_returns_value(self):
        provider = InMemorySecretsProvider({"DB_PASSWORD": "s3cr3t"})
        svc = SecretsService(provider)
        assert svc.get("DB_PASSWORD") == "s3cr3t"

    def test_missing_secret_raises_key_error(self):
        provider = InMemorySecretsProvider()
        svc = SecretsService(provider)
        with pytest.raises(KeyError, match="API_KEY"):
            svc.get("API_KEY")

    def test_set_and_get_secret(self):
        provider = InMemorySecretsProvider()
        provider.set_secret("NEW_SECRET", "value123")
        svc = SecretsService(provider)
        assert svc.get("NEW_SECRET") == "value123"

    def test_env_provider_reads_from_os_environ(self):
        os.environ["TEST_SECRET_VALUE"] = "env-secret"
        try:
            provider = EnvSecretsProvider()
            svc = SecretsService(provider)
            assert svc.get("TEST_SECRET_VALUE") == "env-secret"
        finally:
            os.environ.pop("TEST_SECRET_VALUE", None)

    def test_controller_returns_secret_via_di(self):
        provider = InMemorySecretsProvider({"API_KEY": "abc123"})
        client = build_app(provider)
        r = client.get("/secrets/API_KEY")
        assert r.status_code == 200
        assert r.json()["value"] == "abc123"
