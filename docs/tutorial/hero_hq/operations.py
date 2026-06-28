"""HQ operations — lifecycle hooks and a health check (tutorial step 9)."""

from __future__ import annotations

# --8<-- [start:code]
from lauren import Scope, controller, get, injectable, post_construct, pre_destruct


@injectable(scope=Scope.SINGLETON)
class PowerGrid:
    """Captain Singleton's power grid: warmed up at startup, shut down cleanly.

    ``@post_construct`` runs once when the app starts; ``@pre_destruct`` runs
    once during graceful shutdown, with a bounded timeout.
    """

    def __init__(self) -> None:
        self.online = False

    @post_construct
    async def power_up(self) -> None:
        # Open connection pools, warm caches, etc. Here: flip the lights on.
        self.online = True

    @pre_destruct
    async def power_down(self) -> None:
        # Flush, close pools, say goodbye. Runs on SIGTERM/SIGINT shutdown.
        self.online = False


@controller("/status")
class StatusController:
    def __init__(self, grid: PowerGrid) -> None:
        self.grid = grid

    @get("/")
    async def health(self) -> dict:
        return {"hq": "online" if self.grid.online else "warming up", "grid": self.grid.online}


# --8<-- [end:code]
