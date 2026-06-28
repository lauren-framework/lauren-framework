"""The Door Bouncer — a badge guard + a custom error (tutorial step 5)."""

from __future__ import annotations

# --8<-- [start:code]
from lauren import ExecutionContext, Scope, injectable
from lauren.exceptions import HTTPError

# In production, load valid badges from the environment or a secrets manager —
# never hardcode them. This is a tutorial; the Bouncer is on a budget.
VALID_BADGES = frozenset({"hq-badge-007"})


class VillainDetectedError(HTTPError):
    """403 — someone without a valid HQ badge tried to get in."""

    status_code = 403
    code = "villain_detected"


@injectable(scope=Scope.SINGLETON)
class BadgeGuard:
    """Checks the ``X-HQ-Badge`` header before a protected route runs."""

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        badge = ctx.request.headers.get("x-hq-badge")
        if badge not in VALID_BADGES:
            raise VillainDetectedError(
                "halt! that is not a valid HQ badge",
                detail={"hint": "send a valid X-HQ-Badge header"},
            )
        return True


# --8<-- [end:code]
