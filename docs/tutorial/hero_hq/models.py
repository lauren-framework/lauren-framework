"""The hero dossier — request/response models (tutorial step 2)."""

from __future__ import annotations

# --8<-- [start:code]
from pydantic import BaseModel


class CreateHero(BaseModel):
    """The paperwork a hero files to join HQ."""

    name: str
    power: str
    wattage: int  # raw power level; the Auditor insists it's an int


class HeroOut(BaseModel):
    """What HQ hands back once a hero is on the roster."""

    id: int
    name: str
    power: str
    wattage: int


# --8<-- [end:code]
