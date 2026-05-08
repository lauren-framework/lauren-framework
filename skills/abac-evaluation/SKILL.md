---
name: abac-evaluation
description: Implements Attribute-Based Access Control (ABAC) policy evaluation in Lauren. Use when access decisions must consider multiple attributes of the subject, resource, and environment — not just a static role.
---

> Use `codemap find "injectable"` to locate DI decorator before reading.

# Attribute-Based Access Control (ABAC) Evaluation

## Overview

ABAC evaluates a list of `Policy` objects against a *subject* (the user),
a *resource* (the object being accessed), and an *action* (what they want
to do). Each policy checks `subject[attr] <op> value`. All policies must
pass for access to be granted.

No external dependencies — pure Python dataclasses.

## Core Pattern

```python
from __future__ import annotations

from dataclasses import dataclass, field
from lauren import Scope, controller, get, injectable, module, set_metadata, use_guards, ExecutionContext
from lauren.exceptions import ForbiddenError


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------

OPERATORS = {
    "eq":  lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "in":  lambda a, b: a in b,
    "gt":  lambda a, b: a > b,
    "lt":  lambda a, b: a < b,
}


@dataclass(frozen=True)
class Policy:
    """A single ABAC rule: subject[attr] <op> value."""

    subject_attr: str
    operator: str
    value: object

    def evaluate(self, subject: dict) -> bool:
        op_fn = OPERATORS.get(self.operator)
        if op_fn is None:
            raise ValueError(f"Unknown operator: {self.operator!r}")
        return op_fn(subject.get(self.subject_attr), self.value)


@dataclass
class PolicySet:
    """A named collection of policies; all must pass (AND semantics)."""

    name: str
    policies: list[Policy] = field(default_factory=list)
    actions: set[str] = field(default_factory=set)

    def matches_action(self, action: str) -> bool:
        return not self.actions or action in self.actions

    def evaluate(self, subject: dict) -> bool:
        return all(p.evaluate(subject) for p in self.policies)


@injectable(scope=Scope.SINGLETON)
class ABACEvaluator:
    """Evaluates subject attributes against registered policy sets."""

    def __init__(self) -> None:
        self._policy_sets: list[PolicySet] = []

    def register(self, policy_set: PolicySet) -> None:
        self._policy_sets.append(policy_set)

    def evaluate(self, subject: dict, action: str) -> bool:
        """Return True only if all applicable policy sets pass."""
        applicable = [ps for ps in self._policy_sets if ps.matches_action(action)]
        if not applicable:
            return True  # no policies registered for action → permit
        return all(ps.evaluate(subject) for ps in applicable)


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

ACTION_KEY = "abac_action"


@injectable(scope=Scope.SINGLETON)
class ABACGuard:
    """Reads action from route metadata and evaluates ABAC policies."""

    def __init__(self, evaluator: ABACEvaluator) -> None:
        self._evaluator = evaluator

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        action = ctx.get_metadata(ACTION_KEY, "")
        # Subject attributes come from request state (populated by auth guard)
        subject = ctx.request.state.get("subject", {})
        if not self._evaluator.evaluate(subject, action):
            raise ForbiddenError(
                f"ABAC denied action '{action}'",
                detail={"action": action},
            )
        return True


# ---------------------------------------------------------------------------
# Example app wiring
# ---------------------------------------------------------------------------

@use_guards(ABACGuard)
@controller("/docs")
class DocsController:
    @get("/secret")
    @set_metadata(ACTION_KEY, "read_secret")
    async def secret(self) -> dict:
        return {"content": "top secret"}


@module(controllers=[DocsController], providers=[ABACEvaluator, ABACGuard])
class ABACModule:
    pass
```

## Key Points

- `PolicySet.actions` is empty → applies to all actions.
- Multiple policy sets are all evaluated; access requires every set to pass.
- Store `subject` in `ctx.request.state` from an upstream authentication guard.
- Swap in-memory `_policy_sets` for a database-backed loader without changing the guard interface.
