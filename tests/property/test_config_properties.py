"""Hypothesis property tests for typed-configuration invariants.

Skipped automatically when hypothesis is not installed.
"""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren._config._coerce import coerce  # noqa: E402
from lauren._config._sources import DotenvSyntaxError, parse_dotenv  # noqa: E402
from lauren.config import Secret  # noqa: E402

_TOLERANT = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _render(value: object) -> str:
    """Render a value the way an operator would write it in an environment variable."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(_render(v) for v in value)
    return repr(value) if isinstance(value, float) else str(value)


@_TOLERANT
@given(st.integers())
def test_int_round_trip(value: int) -> None:
    assert coerce(_render(value), int) == value


@_TOLERANT
@given(st.floats(allow_nan=False))
def test_float_round_trip(value: float) -> None:
    assert coerce(_render(value), float) == value


@_TOLERANT
@given(st.booleans())
def test_bool_round_trip(value: bool) -> None:
    assert coerce(_render(value), bool) is value


@_TOLERANT
@given(st.lists(st.integers(), max_size=20))
def test_int_list_round_trip(value: list[int]) -> None:
    assert coerce(_render(value), list[int]) == value


@_TOLERANT
@given(st.text(max_size=400))
def test_dotenv_parser_only_raises_documented_syntax_errors(text: str) -> None:
    try:
        result = parse_dotenv(text)
    except DotenvSyntaxError as exc:
        assert exc.line >= 1
        assert exc.message.startswith(f"line {exc.line}:")
    else:
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in result.items())


@_TOLERANT
@given(
    st.dictionaries(
        st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,15}", fullmatch=True),
        st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=30),
        max_size=10,
    )
)
def test_single_quoted_dotenv_values_round_trip(values: dict[str, str]) -> None:
    clean = {k: v.replace("'", "") for k, v in values.items()}
    text = "\n".join(f"{k}='{v}'" for k, v in clean.items())

    assert parse_dotenv(text) == clean


@_TOLERANT
@given(
    st.one_of(st.text(min_size=1), st.integers(), st.binary(min_size=1), st.lists(st.integers(), min_size=1))
)
def test_secret_repr_never_contains_the_value(value: object) -> None:
    secret = Secret(value)

    assert repr(value) not in repr(secret) or repr(value) in "Secret('**********')"
    assert str(secret) == "Secret('**********')"
