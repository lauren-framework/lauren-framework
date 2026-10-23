"""Unit tests for ``@command`` and ``Opt`` (decoration-time validation only)."""

from __future__ import annotations

from typing import Any

import pytest

from lauren.commands import CommandMeta, Opt, command
from lauren.exceptions import DecoratorUsageError

COMMAND_META = "__lauren_command__"


def test_command_attaches_metadata_and_returns_the_same_function() -> None:
    def seed(self: Any) -> None: ...

    decorated = command("db:seed", help="Seed.", aliases=("seed",), hidden=True)(seed)

    assert decorated is seed
    assert getattr(seed, COMMAND_META) == [CommandMeta("db:seed", "Seed.", ("seed",), True)]


def test_stacking_accumulates_command_names() -> None:
    @command("b")
    @command("a")
    def run(self: Any) -> None: ...

    assert [m.name for m in getattr(run, COMMAND_META)] == ["a", "b"]


@pytest.mark.parametrize("wrapper", [staticmethod, classmethod])
def test_marker_lands_on_the_wrapped_function(wrapper: Any) -> None:
    def run(*args: Any) -> None: ...

    descriptor = wrapper(run)
    result = command("x")(descriptor)

    assert result is descriptor
    assert getattr(run, COMMAND_META)[0].name == "x"


@pytest.mark.parametrize("name", ["db:seed", "a", "0", "users.purge-old", "x_y", "a:b:c"])
def test_valid_names_are_accepted(name: str) -> None:
    def fn(self: Any) -> None: ...

    command(name)(fn)
    assert getattr(fn, COMMAND_META)[0].name == name


@pytest.mark.parametrize("name", ["", "DB:seed", ":seed", "-seed", "db seed", "db/seed", "émoji"])
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        command(name)

    assert info.value.detail["option"] == "name"


def test_bare_usage_is_rejected() -> None:
    def seed(self: Any) -> None: ...

    with pytest.raises(DecoratorUsageError) as info:
        command(seed)

    assert info.value.detail["decorator"] == "command"


def test_missing_name_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        command()

    assert info.value.detail["reason"] == "missing_name"


def test_non_string_name_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError):
        command(42)


def test_aliases_as_a_bare_string_are_rejected() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        command("db:seed", aliases="seed")  # type: ignore[arg-type]

    assert info.value.detail["option"] == "aliases"


def test_invalid_alias_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        command("db:seed", aliases=("Seed",))

    assert info.value.detail["option"] == "alias"


@pytest.mark.parametrize(("option", "value"), [("help", 3), ("hidden", "yes")])
def test_invalid_option_types_are_rejected(option: str, value: Any) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        command("x", **{option: value})

    assert info.value.detail["option"] == option


def test_decorating_a_class_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError):
        command("x")(type("Thing", (), {}))


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_generator_methods_are_rejected(kind: str) -> None:
    if kind == "sync":

        def gen(self: Any) -> Any:
            yield 1

    else:

        async def gen(self: Any) -> Any:
            yield 1

    with pytest.raises(DecoratorUsageError):
        command("x")(gen)


def test_opt_accepts_valid_values() -> None:
    opt = Opt(short="-n", env="SEED_COUNT", help="rows")

    assert (opt.short, opt.env, opt.help) == ("-n", "SEED_COUNT", "rows")


@pytest.mark.parametrize(
    ("kwargs", "option"),
    [
        ({"short": "n"}, "short"),
        ({"short": "--n"}, "short"),
        ({"short": "-nn"}, "short"),
        ({"env": "1BAD"}, "env"),
        ({"env": "WITH SPACE"}, "env"),
        ({"help": 3}, "help"),
    ],
)
def test_opt_rejects_invalid_values(kwargs: dict[str, Any], option: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        Opt(**kwargs)

    assert info.value.detail["option"] == option
