"""Unit tests for the command argument compiler (PRD F19 §5.5).

Each command is compiled by ``LaurenFactory.create`` (Phase 6a); the tests
parse argv with the prebuilt parser and never start the app.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest

from lauren import Inject, LaurenFactory, Token, injectable, module, use_value
from lauren._commands._compile import (
    CompiledCommand,
    _HelpRequested,
    _split_runner_flags,
    _UsageError,
)
from lauren.commands import CommandContext, Opt, command
from lauren.exceptions import DecoratorUsageError, UnresolvableParameterError


class Color(enum.Enum):
    RED = "red"
    DARK_BLUE = "dark-blue"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


GREETING = Token("greeting")


@injectable()
class Greeter:
    pass


@injectable()
class ArgvCommands:
    @command("scalars")
    def scalars(
        self,
        name: str,
        count: int,
        ratio: float,
        amount: decimal.Decimal,
        path: Path,
        ident: uuid.UUID,
        when: datetime.datetime,
        day: datetime.date,
        at: datetime.time,
        wait: datetime.timedelta,
    ) -> None: ...

    @command("optional-positionals")
    def optional_positionals(self, name: str = "anon", extra: int | None = None) -> None: ...

    @command("options")
    def options(
        self,
        *,
        required_opt: str,
        count: Annotated[int, Opt(short="-n", help="rows per table", env="ARGV_COUNT")] = 50,
        label: str | None = None,
        needed: Annotated[int, Opt(env="ARGV_NEEDED")],
    ) -> None: ...

    @command("flags")
    def flags(
        self,
        *,
        reset: bool = False,
        enabled: bool = True,
        maybe: bool | None = None,
        quiet: Annotated[bool, Opt(short="-q", env="ARGV_QUIET")] = False,
    ) -> None: ...

    @command("lists")
    def lists(
        self,
        files: list[Path],
        *,
        tags: list[str] | None = None,
        nums: tuple[int, ...] = (),
        from_env: Annotated[list[int], Opt(env="ARGV_LIST")] = [],  # noqa: B006
    ) -> None: ...

    @command("rest")
    def rest(self, first: str, others: list[str] = []) -> None: ...  # noqa: B006

    @command("choices")
    def choices(
        self,
        color: Color,
        *,
        level: Level = Level.LOW,
        mode: Literal["fast", "slow"] = "fast",
        n: Literal[1, 2] = 1,
    ) -> None: ...

    @command("untyped")
    def untyped(self, a, b=3, *, c=None) -> None: ...  # type: ignore[no-untyped-def]

    @command("mixed")
    def mixed(
        self,
        ctx: CommandContext,
        greeter: Greeter,
        greeting: Annotated[str, Inject(GREETING)],
        target: str,
    ) -> None: ...

    @command("documented")
    def documented(self, *, pct: Annotated[int, Opt(help="100% sure")] = 5) -> None:
        """First line of the docstring.

        More text that is not used.
        """

    @command("explicit", help="Given help.", aliases=("ex", "e"), hidden=True)
    def explicit(self) -> None:
        """Docstring help."""

    @command("own-flags")
    def own_flags(self, *, yes: bool = False) -> None: ...


@module(providers=[ArgvCommands, Greeter, use_value(provide=GREETING, value="hi")])
class ArgvModule:
    pass


@pytest.fixture(scope="module")
def commands() -> Any:
    return LaurenFactory.create(ArgvModule)._commands


def _cmd(commands: Any, name: str) -> CompiledCommand:
    found = commands.lookup(name)
    assert found is not None
    return found


def test_scalar_positionals_are_coerced(commands: Any) -> None:
    ident = uuid.uuid4()
    argv = ["bob", "3", "1.5", "2.50", "out/x.json", str(ident), "2026-01-02T03:04:05", "2026-01-02", "04:05"]
    argv.append("90")

    result = _cmd(commands, "scalars").parse(argv)

    assert result == {
        "name": "bob",
        "count": 3,
        "ratio": 1.5,
        "amount": decimal.Decimal("2.50"),
        "path": Path("out/x.json"),
        "ident": ident,
        "when": datetime.datetime(2026, 1, 2, 3, 4, 5),
        "day": datetime.date(2026, 1, 2),
        "at": datetime.time(4, 5),
        "wait": datetime.timedelta(seconds=90),
    }


@pytest.mark.parametrize(
    ("index", "bad"),
    [(1, "three"), (2, "x"), (3, "1,2,3"), (5, "not-a-uuid"), (6, "yesterday"), (7, "2026-13-01")],
)
def test_invalid_scalar_is_a_usage_error(commands: Any, index: int, bad: str) -> None:
    argv = [
        "bob",
        "3",
        "1.5",
        "2.5",
        "p",
        str(uuid.uuid4()),
        "2026-01-02T03:04:05",
        "2026-01-02",
        "04:05",
        "1",
    ]
    argv[index] = bad

    with pytest.raises(_UsageError) as info:
        _cmd(commands, "scalars").parse(argv)

    assert repr(bad) in info.value.message


def test_missing_positional_is_a_usage_error(commands: Any) -> None:
    with pytest.raises(_UsageError) as info:
        _cmd(commands, "scalars").parse(["bob"])

    assert "required" in info.value.message


def test_defaulted_positionals_are_optional(commands: Any) -> None:
    cmd = _cmd(commands, "optional-positionals")

    assert cmd.parse([]) == {"name": "anon", "extra": None}
    assert cmd.parse(["ada", "5"]) == {"name": "ada", "extra": 5}


def test_keyword_only_parameters_are_options(commands: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARGV_COUNT", raising=False)
    monkeypatch.setenv("ARGV_NEEDED", "1")

    result = _cmd(commands, "options").parse(["--required-opt", "x", "-n", "7"])

    assert result == {"required_opt": "x", "count": 7, "label": None, "needed": 1}


def test_long_option_name_is_kebab_case(commands: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGV_NEEDED", "1")

    result = _cmd(commands, "options").parse(["--required-opt=x", "--count", "9", "--label", "l"])

    assert (result["count"], result["label"]) == (9, "l")


def test_missing_required_option_is_a_usage_error(commands: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGV_NEEDED", "1")

    with pytest.raises(_UsageError) as info:
        _cmd(commands, "options").parse([])

    assert "--required-opt" in info.value.message


def test_required_option_with_env_names_the_variable(commands: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARGV_NEEDED", raising=False)

    with pytest.raises(_UsageError) as info:
        _cmd(commands, "options").parse(["--required-opt", "x"])

    assert "--needed (or set ARGV_NEEDED)" in info.value.message


def test_env_fallback_is_read_at_parse_time(commands: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = _cmd(commands, "options")
    monkeypatch.setenv("ARGV_NEEDED", "2")
    monkeypatch.setenv("ARGV_COUNT", "11")

    first = cmd.parse(["--required-opt", "x"])
    monkeypatch.setenv("ARGV_COUNT", "12")
    second = cmd.parse(["--required-opt", "x"])

    assert (first["count"], second["count"], first["needed"]) == (11, 12, 2)


def test_command_line_wins_over_env(commands: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGV_NEEDED", "2")
    monkeypatch.setenv("ARGV_COUNT", "11")

    result = _cmd(commands, "options").parse(["--required-opt", "x", "-n", "1"])

    assert result["count"] == 1


def test_invalid_env_value_is_a_usage_error(commands: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGV_NEEDED", "2")
    monkeypatch.setenv("ARGV_COUNT", "lots")

    with pytest.raises(_UsageError) as info:
        _cmd(commands, "options").parse(["--required-opt", "x"])

    assert "ARGV_COUNT" in info.value.message


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], {"reset": False, "enabled": True, "maybe": None, "quiet": False}),
        (
            ["--reset", "--no-enabled", "--maybe"],
            {"reset": True, "enabled": False, "maybe": True, "quiet": False},
        ),
        (["--no-maybe", "-q"], {"reset": False, "enabled": True, "maybe": False, "quiet": True}),
        (["--quiet", "--no-quiet"], {"reset": False, "enabled": True, "maybe": None, "quiet": False}),
    ],
)
def test_bool_parameters_are_flag_pairs(
    commands: Any, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: dict[str, Any]
) -> None:
    monkeypatch.delenv("ARGV_QUIET", raising=False)

    assert _cmd(commands, "flags").parse(argv) == expected


@pytest.mark.parametrize(("raw", "expected"), [("yes", True), ("0", False), ("on", True)])
def test_flag_env_fallback_uses_lax_booleans(
    commands: Any, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("ARGV_QUIET", raw)

    assert _cmd(commands, "flags").parse([])["quiet"] is expected


def test_lists_are_variadic_positionals_and_repeatable_options(
    commands: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARGV_LIST", raising=False)

    result = _cmd(commands, "lists").parse(
        ["a", "b", "--tags", "x", "--tags", "y", "--nums", "1", "--nums", "2"]
    )

    assert result == {"files": [Path("a"), Path("b")], "tags": ["x", "y"], "nums": (1, 2), "from_env": []}


def test_absent_repeatable_options_keep_their_defaults(
    commands: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARGV_LIST", raising=False)

    result = _cmd(commands, "lists").parse(["a"])

    assert (result["tags"], result["nums"]) == (None, ())


def test_required_list_positional_needs_one_value(commands: Any) -> None:
    with pytest.raises(_UsageError):
        _cmd(commands, "lists").parse([])


def test_list_env_fallback_splits_on_commas(commands: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGV_LIST", "1, 2,3")

    assert _cmd(commands, "lists").parse(["a"])["from_env"] == [1, 2, 3]


def test_defaulted_list_positional_takes_zero_or_more(commands: Any) -> None:
    cmd = _cmd(commands, "rest")

    assert cmd.parse(["a"]) == {"first": "a", "others": []}
    assert cmd.parse(["a", "b", "c"]) == {"first": "a", "others": ["b", "c"]}


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["dark-blue"], {"color": Color.DARK_BLUE, "level": Level.LOW, "mode": "fast", "n": 1}),
        (["DARK_BLUE"], {"color": Color.DARK_BLUE, "level": Level.LOW, "mode": "fast", "n": 1}),
        (
            ["red", "--level", "2", "--mode", "slow", "--n", "2"],
            {"color": Color.RED, "level": Level.HIGH, "mode": "slow", "n": 2},
        ),
    ],
)
def test_enum_and_literal_parameters_are_choices(
    commands: Any, argv: list[str], expected: dict[str, Any]
) -> None:
    assert _cmd(commands, "choices").parse(argv) == expected


@pytest.mark.parametrize("argv", [["purple"], ["red", "--mode", "medium"], ["red", "--n", "3"]])
def test_values_outside_the_choices_are_usage_errors(commands: Any, argv: list[str]) -> None:
    with pytest.raises(_UsageError):
        _cmd(commands, "choices").parse(argv)


def test_unannotated_parameters_are_strings_or_take_the_default_type(commands: Any) -> None:
    cmd = _cmd(commands, "untyped")

    assert cmd.parse(["x"]) == {"a": "x", "b": 3, "c": None}
    assert cmd.parse(["x", "7", "--c", "z"]) == {"a": "x", "b": 7, "c": "z"}


def test_injected_parameters_are_not_cli_arguments(commands: Any) -> None:
    cmd = _cmd(commands, "mixed")

    assert [p.name for p in cmd.info.parameters] == ["target"]
    assert cmd.parse(["t"]) == {"target": "t"}


def test_unknown_arguments_are_usage_errors(commands: Any) -> None:
    with pytest.raises(_UsageError) as info:
        _cmd(commands, "rest").parse(["a", "--nope"])

    assert "--nope" in info.value.message


def test_help_flag_is_reported(commands: Any) -> None:
    with pytest.raises(_HelpRequested):
        _cmd(commands, "options").parse(["--required-opt", "x", "--help"])


def test_generated_help_describes_every_parameter(commands: Any) -> None:
    text = _cmd(commands, "options").format_help()

    assert "usage: options" in text
    assert "-n" in text and "--count COUNT" in text  # 3.13+ prints "-n, --count COUNT"
    assert "rows per table (default: 50; env: ARGV_COUNT)" in text
    assert "--required-opt REQUIRED_OPT" in text


def test_help_for_choices_and_flags(commands: Any) -> None:
    choices = _cmd(commands, "choices").format_help()
    flags = _cmd(commands, "flags").format_help()

    assert "one of: red, dark-blue" in choices
    assert "--mode {fast,slow}" in choices
    assert "--reset, --no-reset" in flags
    assert "default: on" in flags


def test_help_text_comes_from_the_docstring_and_percent_is_literal(commands: Any) -> None:
    cmd = _cmd(commands, "documented")
    text = cmd.format_help()

    assert cmd.info.help == "First line of the docstring."
    assert "First line of the docstring." in text
    assert "More text" not in text
    assert "100% sure" in text


def test_explicit_help_aliases_and_hidden_are_recorded(commands: Any) -> None:
    cmd = _cmd(commands, "explicit")

    assert (cmd.info.help, cmd.info.aliases, cmd.info.hidden) == ("Given help.", ("ex", "e"), True)
    assert commands.lookup("ex") is cmd
    assert commands.lookup("e") is cmd


def test_command_info_describes_parameters(commands: Any) -> None:
    by_name = {p.name: p for p in _cmd(commands, "options").info.parameters}

    count = by_name["count"]
    assert (count.kind, count.flags, count.type, count.required, count.default) == (
        "option",
        ("-n", "--count"),
        "int",
        False,
        50,
    )
    assert (count.env, count.help, count.repeatable, count.choices) == (
        "ARGV_COUNT",
        "rows per table",
        False,
        (),
    )
    assert by_name["required_opt"].required is True
    assert by_name["required_opt"].default is None


def test_command_info_names_module_and_target(commands: Any) -> None:
    info = _cmd(commands, "lists").info

    assert info.module is ArgvModule
    assert info.target == "ArgvCommands.lists"
    assert [(p.name, p.kind, p.repeatable) for p in info.parameters] == [
        ("files", "positional", True),
        ("tags", "option", True),
        ("nums", "option", True),
        ("from_env", "option", True),
    ]


def test_choices_are_listed_in_command_info(commands: Any) -> None:
    by_name = {p.name: p for p in _cmd(commands, "choices").info.parameters}

    assert by_name["color"].choices == ("red", "dark-blue")
    assert by_name["mode"].choices == ("fast", "slow")
    assert by_name["level"].choices == ("1", "2")


def test_listing_hides_hidden_commands_and_sorts_by_name(commands: Any) -> None:
    visible = [info.name for info in commands.infos()]
    everything = [info.name for info in commands.infos(include_hidden=True)]

    assert "explicit" not in visible
    assert "explicit" in everything
    assert visible == sorted(visible)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["a", "--yes", "b"], (["a", "b"], True, False)),
        (["--verbose", "a"], (["a"], False, True)),
        (["a", "--", "--yes"], (["a", "--", "--yes"], False, False)),
    ],
)
def test_runner_flags_are_split_off(commands: Any, argv: list[str], expected: Any) -> None:
    assert _split_runner_flags(_cmd(commands, "rest"), argv) == expected


def test_a_command_declaring_yes_keeps_it(commands: Any) -> None:
    cmd = _cmd(commands, "own-flags")

    args, assume_yes, _ = _split_runner_flags(cmd, ["--yes"])

    assert (args, assume_yes) == (["--yes"], False)
    assert cmd.parse(args) == {"yes": True}


# ---------------------------------------------------------------------------
# Startup errors
# ---------------------------------------------------------------------------


def _build_with(method: Any) -> None:
    namespace = {"run": command("bad")(method)}
    cls = injectable()(type("BadCommands", (), namespace))
    root = module(providers=[cls])(type("BadModule", (), {}))
    LaurenFactory.create(root)


@pytest.mark.parametrize(
    "annotation",
    [dict[str, int], tuple[int, int], int | str, list[list[int]], list[int | None], object, bytes],
)
def test_unsupported_parameter_types_fail_startup(annotation: Any) -> None:
    def run(self: Any, value: Any) -> None: ...

    run.__annotations__["value"] = annotation

    with pytest.raises(UnresolvableParameterError) as info:
        _build_with(run)

    assert info.value.detail["command"] == "bad"
    assert info.value.detail["param"] == "value"
    assert info.value.detail["reason"] == "unsupported_command_parameter"


def test_short_flag_on_a_positional_fails_startup() -> None:
    def run(self: Any, value: Annotated[int, Opt(short="-v")]) -> None: ...

    with pytest.raises(DecoratorUsageError) as info:
        _build_with(run)

    assert info.value.detail["reason"] == "opt_on_positional"


def test_help_on_a_positional_is_allowed() -> None:
    def run(self: Any, value: Annotated[int, Opt(help="the value")]) -> None: ...

    _build_with(run)


def test_conflicting_short_flags_fail_startup() -> None:
    def run(
        self: Any, *, a: Annotated[int, Opt(short="-x")] = 1, b: Annotated[int, Opt(short="-x")] = 2
    ) -> None: ...

    with pytest.raises(DecoratorUsageError) as info:
        _build_with(run)

    assert info.value.detail["reason"] == "conflicting_option"


def test_an_option_named_help_conflicts_with_help() -> None:
    def run(self: Any, *, help: str = "") -> None: ...  # noqa: A002

    with pytest.raises(DecoratorUsageError) as info:
        _build_with(run)

    assert info.value.detail["param"] == "help"
