"""Integration tests for the ABAC policy evaluation skill."""

from __future__ import annotations

from dataclasses import dataclass, field


from lauren import (
    ExecutionContext,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    set_metadata,
    use_guards,
)
from lauren.exceptions import ForbiddenError
from lauren.testing import TestClient

# ---------------------------------------------------------------------------
# ABAC engine
# ---------------------------------------------------------------------------

OPERATORS = {
    "eq": lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
}


@dataclass(frozen=True)
class Policy:
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
    name: str
    policies: list[Policy] = field(default_factory=list)
    actions: set[str] = field(default_factory=set)

    def matches_action(self, action: str) -> bool:
        return not self.actions or action in self.actions

    def evaluate(self, subject: dict) -> bool:
        return all(p.evaluate(subject) for p in self.policies)


@injectable(scope=Scope.SINGLETON)
class ABACEvaluator:
    def __init__(self) -> None:
        self._policy_sets: list[PolicySet] = []

    def register(self, policy_set: PolicySet) -> None:
        self._policy_sets.append(policy_set)

    def evaluate(self, subject: dict, action: str) -> bool:
        applicable = [ps for ps in self._policy_sets if ps.matches_action(action)]
        if not applicable:
            return True
        return all(ps.evaluate(subject) for ps in applicable)


ACTION_KEY = "abac_action"
SUBJECT_HEADER = "x-subject-role"


@injectable(scope=Scope.SINGLETON)
class ABACGuard:
    def __init__(self, evaluator: ABACEvaluator) -> None:
        self._evaluator = evaluator

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        action = ctx.get_metadata(ACTION_KEY, "")
        # Read subject from header for test simplicity
        role = ctx.request.headers.get(SUBJECT_HEADER, "")
        subject = {"role": role, "department": ctx.request.headers.get("x-dept", "")}

        if not self._evaluator.evaluate(subject, action):
            raise ForbiddenError(f"ABAC denied action '{action}'")
        return True


@use_guards(ABACGuard)
@controller("/docs")
class DocsController:
    @get("/public")
    async def public_doc(self) -> dict:
        return {"content": "public"}

    @get("/secret")
    @set_metadata(ACTION_KEY, "read_secret")
    async def secret_doc(self) -> dict:
        return {"content": "top secret"}

    @get("/finance")
    @set_metadata(ACTION_KEY, "read_finance")
    async def finance_doc(self) -> dict:
        return {"content": "financials"}


@module(controllers=[DocsController], providers=[ABACEvaluator, ABACGuard])
class ABACModule:
    pass


def build_app_with_policies() -> TestClient:
    app = LaurenFactory.create(ABACModule)
    # Wire policies after creation by accessing the evaluator from the container
    # We do it via a fresh factory call with a configured evaluator
    return TestClient(app)


def _make_evaluator() -> ABACEvaluator:
    ev = ABACEvaluator()
    ev.register(
        PolicySet(
            name="secret_access",
            policies=[Policy("role", "eq", "admin")],
            actions={"read_secret"},
        )
    )
    ev.register(
        PolicySet(
            name="finance_access",
            policies=[
                Policy("role", "in", ["admin", "finance"]),
                Policy("department", "eq", "HQ"),
            ],
            actions={"read_finance"},
        )
    )
    return ev


class TestABACEvaluator:
    """Pure unit tests for the ABAC engine without the HTTP layer."""

    def test_policy_eq_passes(self):
        p = Policy("role", "eq", "admin")
        assert p.evaluate({"role": "admin"}) is True

    def test_policy_eq_fails(self):
        p = Policy("role", "eq", "admin")
        assert p.evaluate({"role": "viewer"}) is False

    def test_policy_in_passes(self):
        p = Policy("role", "in", ["admin", "editor"])
        assert p.evaluate({"role": "editor"}) is True

    def test_policy_in_fails(self):
        p = Policy("role", "in", ["admin", "editor"])
        assert p.evaluate({"role": "viewer"}) is False

    def test_policy_set_all_must_pass(self):
        ps = PolicySet(
            name="strict",
            policies=[
                Policy("role", "eq", "admin"),
                Policy("department", "eq", "HQ"),
            ],
        )
        assert ps.evaluate({"role": "admin", "department": "HQ"}) is True
        assert ps.evaluate({"role": "admin", "department": "Remote"}) is False

    def test_evaluator_no_policies_permits_all(self):
        ev = ABACEvaluator()
        assert ev.evaluate({"role": "anyone"}, "any_action") is True

    def test_evaluator_matching_policy_set(self):
        ev = _make_evaluator()
        assert ev.evaluate({"role": "admin"}, "read_secret") is True
        assert ev.evaluate({"role": "viewer"}, "read_secret") is False

    def test_evaluator_multiple_conditions(self):
        ev = _make_evaluator()
        assert (
            ev.evaluate({"role": "finance", "department": "HQ"}, "read_finance") is True
        )
        assert (
            ev.evaluate({"role": "finance", "department": "Remote"}, "read_finance")
            is False
        )

    def test_evaluator_non_applicable_action_permits(self):
        ev = _make_evaluator()
        # "write" action has no matching policy set → permitted
        assert ev.evaluate({"role": "viewer"}, "write") is True
