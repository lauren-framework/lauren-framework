---
name: feature-flags
description: Implements a feature flag service with per-user rollout percentages using consistent hashing. Use when you need gradual feature rollouts, A/B testing, or kill-switch toggles in a Lauren app.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Feature Flag Service with Rollout Percentages

## Overview

`FeatureFlagService` maintains a registry of flags, each with an `enabled`
boolean and a `rollout_pct` (0–100). `is_enabled(flag, user_id)` uses a
deterministic MD5 hash of `"flag:user_id"` so the same user always gets the
same result for a given rollout percentage. No external dependencies.

## FeatureFlagService

```python
import hashlib
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class FeatureFlagService:
    def __init__(self) -> None:
        self._flags: dict[str, dict] = {}

    def register(self, name: str, enabled: bool = True,
                 rollout_pct: float = 100.0) -> None:
        """Register or update a feature flag."""
        self._flags[name] = {"enabled": enabled, "rollout_pct": rollout_pct}

    def is_enabled(self, flag: str, user_id: str = "") -> bool:
        f = self._flags.get(flag)
        if not f or not f["enabled"]:
            return False
        if f["rollout_pct"] >= 100.0:
            return True
        if f["rollout_pct"] <= 0.0:
            return False
        bucket = int(
            hashlib.md5(f"{flag}:{user_id}".encode()).hexdigest(), 16
        ) % 100
        return bucket < f["rollout_pct"]

    def all_flags(self) -> dict[str, dict]:
        return dict(self._flags)
```

## Controller

```python
from lauren import controller, get, Path, module

@controller("/flags")
class FeatureFlagController:
    def __init__(self, flags: FeatureFlagService) -> None:
        self._flags = flags

    @get("/{flag_name}/{user_id}")
    async def check(self, flag_name: Path[str], user_id: Path[str]) -> dict:
        return {
            "flag": flag_name,
            "user_id": user_id,
            "enabled": self._flags.is_enabled(flag_name, user_id),
        }

    @get("/")
    async def list_flags(self) -> dict:
        return self._flags.all_flags()

@module(
    controllers=[FeatureFlagController],
    providers=[FeatureFlagService],
)
class FeatureFlagModule:
    pass
```

## Seeding flags at startup

Seed flags in the controller or a dedicated `@post_construct` hook:

```python
from lauren import post_construct

@injectable(scope=Scope.SINGLETON)
class FlagSeedService:
    def __init__(self, flags: FeatureFlagService) -> None:
        self._flags = flags

    @post_construct
    async def seed(self) -> None:
        self._flags.register("new-checkout-flow", enabled=True, rollout_pct=25.0)
        self._flags.register("dark-mode", enabled=True, rollout_pct=100.0)
        self._flags.register("beta-api", enabled=False)
```

## Rollout distribution

The MD5 hash distributes users uniformly across 0–99 buckets. A 50% rollout
enables the flag for exactly the same set of users on every call — the same
`(flag, user_id)` pair always maps to the same bucket.

```python
# Verify distribution is roughly uniform
enabled = sum(
    1 for uid in range(10_000)
    if svc.is_enabled("my-flag", str(uid))
)
assert 4500 < enabled < 5500  # ~50% ± 5%
```

## Remote flag storage (advanced)

Replace the in-process dict with a Redis hash for multi-worker consistency:

```python
import json, redis

class RedisFlagStore:
    def __init__(self, client: redis.Redis) -> None:
        self._r = client

    def register(self, name: str, enabled: bool, rollout_pct: float) -> None:
        self._r.hset("feature_flags", name,
                     json.dumps({"enabled": enabled, "rollout_pct": rollout_pct}))

    def get(self, name: str) -> dict | None:
        val = self._r.hget("feature_flags", name)
        return json.loads(val) if val else None
```

## Common mistakes

- Using `random.random()` instead of consistent hashing — the result changes
  between calls so the same user sees different behaviour on reload.
- Not checking `enabled=False` first — a flag with `rollout_pct=100` but
  `enabled=False` should always be off; check `enabled` before `rollout_pct`.
- Registering flags inside a `REQUEST`-scoped provider — flags are global state
  and belong in a `SINGLETON`.
- Forgetting thread-safety: because sync handlers run in a thread pool, use a
  `threading.Lock` if you mutate `_flags` at runtime from a handler.
