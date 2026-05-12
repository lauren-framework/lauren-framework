"""Integration tests for the feature flag service with rollout percentages (Skill 17).

Pure Python — no external dependencies.
"""

from __future__ import annotations

import hashlib

from lauren import LaurenFactory, Path, Scope, controller, get, injectable, module
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# FeatureFlagService
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class FeatureFlagService:
    def __init__(self) -> None:
        self._flags: dict[str, dict] = {}

    def register(self, name: str, enabled: bool = True, rollout_pct: float = 100.0) -> None:
        self._flags[name] = {"enabled": enabled, "rollout_pct": rollout_pct}

    def is_enabled(self, flag: str, user_id: str = "") -> bool:
        f = self._flags.get(flag)
        if not f or not f["enabled"]:
            return False
        if f["rollout_pct"] >= 100.0:
            return True
        if f["rollout_pct"] <= 0.0:
            return False
        bucket = int(hashlib.md5(f"{flag}:{user_id}".encode()).hexdigest(), 16) % 100
        return bucket < f["rollout_pct"]

    def all_flags(self) -> dict[str, dict]:
        return dict(self._flags)


# ---------------------------------------------------------------------------
# Controller + module
# ---------------------------------------------------------------------------


@controller("/flags")
class FeatureFlagController:
    def __init__(self, flags: FeatureFlagService) -> None:
        self._flags = flags

    @get("/{flag_name}/{user_id}")
    async def check(self, flag_name: Path[str], user_id: Path[str]) -> dict:
        return {
            "flag": flag_name,
            "enabled": self._flags.is_enabled(flag_name, user_id),
        }


@module(controllers=[FeatureFlagController], providers=[FeatureFlagService])
class FeatureFlagModule:
    pass


def build_app():
    return TestClient(LaurenFactory.create(FeatureFlagModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFeatureFlagService:
    def _svc(self) -> FeatureFlagService:
        svc = FeatureFlagService()
        return svc

    def test_0_pct_rollout_never_enables(self):
        svc = self._svc()
        svc.register("dark-mode", enabled=True, rollout_pct=0.0)
        for uid in ["user1", "user2", "user3", "12345"]:
            assert svc.is_enabled("dark-mode", uid) is False

    def test_100_pct_rollout_always_enables(self):
        svc = self._svc()
        svc.register("new-ui", enabled=True, rollout_pct=100.0)
        for uid in ["user1", "user2", "user3", "99999"]:
            assert svc.is_enabled("new-ui", uid) is True

    def test_disabled_flag_never_enables_regardless_of_pct(self):
        svc = self._svc()
        svc.register("beta", enabled=False, rollout_pct=100.0)
        assert svc.is_enabled("beta", "user1") is False

    def test_50_pct_is_consistent_per_user(self):
        svc = self._svc()
        svc.register("half-roll", enabled=True, rollout_pct=50.0)
        for uid in ["alice", "bob", "charlie", "dave"]:
            first = svc.is_enabled("half-roll", uid)
            second = svc.is_enabled("half-roll", uid)
            assert first == second, f"Result for {uid} changed between calls"

    def test_50_pct_distributes_roughly_half(self):
        svc = self._svc()
        svc.register("half-roll", enabled=True, rollout_pct=50.0)
        enabled = sum(1 for i in range(1000) if svc.is_enabled("half-roll", str(i)))
        assert 400 < enabled < 600, f"Expected ~50% but got {enabled}/1000"

    def test_unknown_flag_returns_false(self):
        svc = self._svc()
        assert svc.is_enabled("nonexistent", "user1") is False

    def test_controller_check_endpoint(self):
        client = build_app()
        # Without registering a flag it should return False (not found)
        r = client.get("/flags/my-flag/user42")
        assert r.status_code == 200
        assert r.json()["enabled"] is False
