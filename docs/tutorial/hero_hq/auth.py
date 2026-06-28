"""Who are you, really? — hero login via sessions (tutorial step 6)."""

from __future__ import annotations

# --8<-- [start:code]
from pydantic import BaseModel

from lauren import Json, Session, controller, get, post
from lauren.exceptions import UnauthorizedError

from .roster import HeroRepository


class LoginBody(BaseModel):
    hero_id: int


@controller("/me")
class IdentityController:
    def __init__(self, roster: HeroRepository) -> None:
        self.roster = roster

    @get("/")
    async def whoami(self, session: Session) -> dict:
        if "hero_id" not in session:
            return {"identified": False}
        return {"identified": True, "hero_id": session["hero_id"], "name": session["name"]}

    @post("/login")
    async def login(self, session: Session, body: Json[LoginBody]) -> dict:
        hero = self.roster.get(body.hero_id)
        if hero is None:
            raise UnauthorizedError("no such hero to log in as", detail={"hero_id": body.hero_id})
        # Rotate the session id on privilege change — the fixation defence.
        session.regenerate_id()
        session["hero_id"] = hero["id"]
        session["name"] = hero["name"]
        return {"welcome": hero["name"]}

    @post("/logout")
    async def logout(self, session: Session) -> dict:
        session.invalidate()
        return {"farewell": True}


# --8<-- [end:code]
