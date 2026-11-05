"""``StateKey[T]`` on ``State`` / ``AppState`` and ``@provides_state`` (F23 §5.2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from lauren import (
    DecoratorUsageError,
    MissingStateError,
    StateKey,
    UnauthorizedError,
    provides_state,
)
from lauren._metadata import PROVIDES_STATE_META, provided_state, state_name
from lauren.extractors import Extraction, parse_extractor_hint, prepare_state_extraction, state_key_of
from lauren.types import AppState, State


@dataclass
class User:
    id: str


CurrentUser = StateKey[User]("test.user")


def test_item_access_stores_under_the_key_name() -> None:
    state = State()

    state[CurrentUser] = User("u1")

    assert state[CurrentUser] == User("u1")
    assert state.get("test.user") == User("u1")
    assert state.asdict() == {"test.user": User("u1")}


def test_identifier_names_stay_readable_as_attributes() -> None:
    key = StateKey[int]("tenant_id")
    state = State()

    state[key] = 7

    assert state.tenant_id == 7


def test_missing_key_raises_missing_state_error() -> None:
    with pytest.raises(MissingStateError) as info:
        State()[CurrentUser]

    assert info.value.status_code == 500
    assert info.value.detail == {"key": "test.user"}


def test_missing_string_item_raises_missing_state_error() -> None:
    with pytest.raises(MissingStateError):
        State()["absent"]


def test_string_items_work_too() -> None:
    state = State()
    state["plain"] = 1

    assert state["plain"] == 1
    assert "plain" in state


@pytest.mark.parametrize(
    "factory", [UnauthorizedError, lambda: UnauthorizedError("login", challenge="Bearer")]
)
def test_on_missing_controls_the_error(factory: Any) -> None:
    key = StateKey[User]("test.auth", on_missing=factory)

    with pytest.raises(UnauthorizedError):
        State()[key]


def test_get_contains_has_and_set_accept_keys() -> None:
    state = State()

    assert state.get(CurrentUser) is None
    assert state.get(CurrentUser, User("anon")) == User("anon")
    assert CurrentUser not in state
    assert not state.has(CurrentUser)

    state.set(CurrentUser, User("u2"))

    assert CurrentUser in state
    assert state.has(CurrentUser)


def test_app_state_supports_keys_until_sealed() -> None:
    config_key = StateKey[str]("test.region")
    app_state = AppState()
    app_state[config_key] = "eu"

    app_state.seal()

    assert app_state[config_key] == "eu"
    with pytest.raises(RuntimeError):
        app_state[config_key] = "us"
    with pytest.raises(RuntimeError):
        app_state.set(config_key, "us")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"name": ""}, "invalid_state_key"),
        ({"name": "k", "on_missing": ValueError}, "invalid_on_missing"),
        ({"name": "k", "on_missing": UnauthorizedError()}, "invalid_on_missing"),
        ({"name": "k", "on_missing": lambda reason: UnauthorizedError(reason)}, "invalid_on_missing"),
        ({"name": "k", "on_missing": 3}, "invalid_on_missing"),
    ],
)
def test_invalid_state_key_declarations(kwargs: dict[str, Any], reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        StateKey(**kwargs)

    assert info.value.detail["reason"] == reason


def test_factory_returning_a_non_http_error_is_a_type_error() -> None:
    key = StateKey[int]("k", on_missing=lambda: ValueError("x"))  # type: ignore[arg-type,return-value]

    with pytest.raises(TypeError):
        key.missing_error()


def test_missing_error_names_the_parameter() -> None:
    error = CurrentUser.missing_error(param="user")

    assert error.detail == {"key": "test.user", "param": "user"}


def test_state_key_is_immutable_and_has_a_repr() -> None:
    assert repr(CurrentUser) == "StateKey('test.user')"
    assert state_name(CurrentUser) == "test.user"
    assert state_name("raw") == "raw"
    with pytest.raises(AttributeError):
        CurrentUser.name = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Annotated[T, Key] parsing
# ---------------------------------------------------------------------------


def test_annotated_key_is_the_state_key_source() -> None:
    source, inner, reads_body, marker, fd, pipes = parse_extractor_hint(Annotated[User, CurrentUser])

    assert (source, inner, reads_body, marker, fd, pipes) == ("state_key", User, False, None, None, ())
    assert state_key_of(Annotated[User, CurrentUser]) is CurrentUser
    assert state_key_of(Annotated[User, CurrentUser] | None) is CurrentUser
    assert state_key_of(User) is None
    assert state_key_of(Annotated[User, "doc"]) is None


@pytest.mark.parametrize(
    "annotation",
    ["Annotated[Query[int], CurrentUser]", "Annotated[int, CurrentUser, Query]"],
)
def test_state_key_cannot_combine_with_an_extractor(annotation: str) -> None:
    from lauren import ExtractorError
    from lauren.extractors import Query

    hint = eval(annotation, {"Annotated": Annotated, "Query": Query, "CurrentUser": CurrentUser})

    with pytest.raises(ExtractorError):
        parse_extractor_hint(hint)


@pytest.mark.parametrize("second", [CurrentUser, StateKey[User]("another.user")])
def test_multiple_state_keys_in_one_annotation_fail_at_compile_time(second: StateKey[User]) -> None:
    from lauren import ExtractorError

    with pytest.raises(ExtractorError, match="multiple StateKey"):
        parse_extractor_hint(Annotated[User, CurrentUser, second])


def _state_ext(annotation: Any, *, has_default: bool = False, default: Any = None) -> Extraction:
    source, inner, _, _, fd, _ = parse_extractor_hint(annotation)
    ext = Extraction(
        name="user",
        source=source or "",
        inner_type=inner,
        field_descriptor=fd,
        default=default,
        has_default=has_default,
    )
    prepare_state_extraction(ext, annotation)
    return ext


@pytest.mark.parametrize(
    ("annotation", "has_default"),
    [
        (Annotated[User, CurrentUser], False),
        (Annotated[User | None, CurrentUser], True),
        (Annotated[User, CurrentUser] | None, True),
    ],
)
def test_optional_state_parameters_default_to_none(annotation: Any, has_default: bool) -> None:
    ext = _state_ext(annotation)

    assert ext.state_key is CurrentUser
    assert ext.has_default is has_default
    assert ext.field_descriptor is None
    if has_default:
        assert ext.default is None


def test_legacy_state_parameter_gets_a_key_under_missing_state_error() -> None:
    from lauren.validation import ValidationPolicy

    ext = Extraction(
        name="tenant", source="state", inner_type=str, field_descriptor=None, default=None, has_default=False
    )

    prepare_state_extraction(ext, str, policy=ValidationPolicy(missing_state="error"))

    assert ext.state_key is not None and ext.state_key.name == "tenant"


def test_legacy_state_parameter_keeps_no_key_by_default() -> None:
    from lauren.validation import ValidationPolicy

    ext = Extraction(
        name="tenant", source="state", inner_type=str, field_descriptor=None, default=None, has_default=False
    )

    prepare_state_extraction(ext, str, policy=ValidationPolicy())

    assert ext.state_key is None


def test_validation_policy_rejects_unknown_missing_state_mode() -> None:
    from lauren import StartupError
    from lauren.validation import ValidationPolicy

    with pytest.raises(StartupError) as info:
        ValidationPolicy(missing_state="ignore")  # type: ignore[arg-type]

    assert info.value.detail["field"] == "missing_state"


# ---------------------------------------------------------------------------
# @provides_state
# ---------------------------------------------------------------------------


def test_provides_state_accumulates_on_the_own_class() -> None:
    other = StateKey[str]("test.tenant")

    @provides_state(CurrentUser)
    @provides_state(other, CurrentUser)
    class Guard:
        async def can_activate(self, ctx: Any) -> bool:
            return True

    class SubGuard(Guard): ...

    assert provided_state(Guard) == (other, CurrentUser)
    assert provided_state(SubGuard) == ()
    assert PROVIDES_STATE_META in Guard.__dict__


@pytest.mark.parametrize("args", [(), ("not-a-key",)])
def test_provides_state_rejects_bad_arguments(args: tuple[Any, ...]) -> None:
    with pytest.raises(DecoratorUsageError):
        provides_state(*args)


def test_provides_state_rejects_bare_usage() -> None:
    with pytest.raises(DecoratorUsageError):

        @provides_state  # type: ignore[arg-type]
        class Guard: ...


def test_provides_state_decorates_classes_only() -> None:
    with pytest.raises(DecoratorUsageError):
        provides_state(CurrentUser)(lambda: None)
