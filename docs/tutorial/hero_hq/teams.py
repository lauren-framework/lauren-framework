"""Teams — the module graph (tutorial steps 4-7).

A Lauren ``@module`` is a superhero team: it declares what it ``provides``,
what it ``exports`` for other teams, and what it ``imports`` from them. As HQ
grows, each feature becomes its own team, and ``HeroHQModule`` assembles them
all under one roof.
"""

from __future__ import annotations

from lauren import module

from .auth import IdentityController
from .dispatch import HeroController
from .mission_control import CommsGateway, CommsRoom, MissionControlController, MissionLog
from .operations import PowerGrid, StatusController
from .roster import HeroRepository


@module(providers=[HeroRepository], exports=[HeroRepository])
class RosterModule:
    """Owns and shares HQ's roster."""


@module(controllers=[HeroController], imports=[RosterModule])
class DispatchModule:
    """The dispatch desk — recruits and lists heroes."""


@module(controllers=[IdentityController], imports=[RosterModule])
class IdentityModule:
    """The badge office — hero login / logout via sessions."""


@module(
    controllers=[MissionControlController, CommsGateway],
    providers=[MissionLog, CommsRoom],
)
class MissionControlModule:
    """The live-ops room — SSE feed, dispatch tasks, and team comms."""


@module(controllers=[StatusController], providers=[PowerGrid])
class OperationsModule:
    """The power grid — lifecycle hooks and a health check."""


@module(imports=[DispatchModule, IdentityModule, MissionControlModule, OperationsModule])
class HeroHQModule:
    """All of Hero HQ, assembled."""
