"""The HQ roster — a shared, in-memory store of heroes (tutorial step 3)."""

from __future__ import annotations

# --8<-- [start:code]
from lauren import Scope, injectable


@injectable(scope=Scope.SINGLETON)
class HeroRepository:
    """HQ's memory. Exactly one of these for the whole app's life, so every
    dispatcher reads and writes the same roster."""

    def __init__(self) -> None:
        self._heroes: dict[int, dict] = {}
        self._next_id = 1

    def recruit(self, name: str, power: str, wattage: int) -> dict:
        hero = {"id": self._next_id, "name": name, "power": power, "wattage": wattage}
        self._heroes[self._next_id] = hero
        self._next_id += 1
        return hero

    def get(self, hero_id: int) -> dict | None:
        return self._heroes.get(hero_id)

    def roster(self) -> list[dict]:
        return list(self._heroes.values())


# --8<-- [end:code]
