"""The front desk — the HTTP controller for heroes (tutorial steps 1-3)."""

from __future__ import annotations

from lauren import Json, Path, controller, get, post, use_guards
from lauren.exceptions import HTTPError

from .models import CreateHero, HeroOut
from .roster import HeroRepository
from .security import BadgeGuard


class HeroNotFoundError(HTTPError):
    """Raised when someone asks for a hero who isn't on the roster."""

    status_code = 404
    code = "hero_not_found"


@controller("/heroes", tags=["heroes"])
class HeroController:
    def __init__(self, roster: HeroRepository) -> None:
        # Lauren injects the shared HeroRepository — see step 3.
        self.roster = roster

    @get("/")
    async def list_heroes(self) -> list[HeroOut]:
        return [HeroOut(**hero) for hero in self.roster.roster()]

    @get("/{id}")
    async def get_hero(self, id: Path[int]) -> HeroOut:
        hero = self.roster.get(id)
        if hero is None:
            raise HeroNotFoundError("no such hero", detail={"id": id})
        return HeroOut(**hero)

    @post("/")
    @use_guards(BadgeGuard)
    async def recruit(self, body: Json[CreateHero]) -> tuple[HeroOut, int]:
        # Recruiting changes the roster, so the Door Bouncer checks a badge first.
        hero = self.roster.recruit(body.name, body.power, body.wattage)
        return HeroOut(**hero), 201
