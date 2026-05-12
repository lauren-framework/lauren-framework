"""Unit tests for ``Optional[Path[T]]`` / ``Path[Optional[T]]`` parsing.

The extractor hint parser recognises two canonical nullable shapes:

* **Outer optional** \u2014 ``Optional[Path[int]]`` or ``Path[int] | None``.
  The union is peeled at parse time and a ``FieldDescriptor(default=None)``
  is synthesised so a missing value produces ``None`` instead of raising.

* **Inner optional** \u2014 ``Path[Optional[int]]``. The inner type is
  carried through verbatim; scalar coercion threads ``None`` values
  correctly, and the extraction layer treats a missing value as
  ``None`` via the same optional-default mechanism.

Both shapes must be fully equivalent from the handler's perspective \u2014
the request receives ``None`` when the parameter is absent and a
correctly-coerced value when it is present.
"""

from __future__ import annotations

from typing import Optional, Union

from lauren.extractors import (
    Cookie,
    FieldDescriptor,
    Header,
    Path,
    Query,
    _coerce_scalar,
    _peel_optional,
    parse_extractor_hint,
)


# ---------------------------------------------------------------------------
# ``_peel_optional`` \u2014 the foundational helper
# ---------------------------------------------------------------------------


def test_peel_optional_strips_none_branch() -> None:
    unwrapped, is_opt = _peel_optional(Optional[int])
    assert unwrapped is int
    assert is_opt is True


def test_peel_optional_strips_pep604_none_branch() -> None:
    unwrapped, is_opt = _peel_optional(int | None)
    assert unwrapped is int
    assert is_opt is True


def test_peel_optional_preserves_non_optional_types() -> None:
    unwrapped, is_opt = _peel_optional(int)
    assert unwrapped is int
    assert is_opt is False


def test_peel_optional_preserves_multi_branch_union_without_none() -> None:
    unwrapped, is_opt = _peel_optional(Union[int, str])
    assert unwrapped == Union[int, str]
    assert is_opt is False


def test_peel_optional_collapses_multi_branch_union_with_none() -> None:
    unwrapped, is_opt = _peel_optional(Union[int, str, None])
    assert unwrapped == Union[int, str]
    assert is_opt is True


def test_peel_optional_collapses_pep604_multi_branch_with_none() -> None:
    unwrapped, is_opt = _peel_optional(int | str | None)
    # Rebuilt as PEP 604 union.
    assert unwrapped == (int | str)
    assert is_opt is True


# ---------------------------------------------------------------------------
# ``parse_extractor_hint`` \u2014 outer optional
# ---------------------------------------------------------------------------


def test_outer_optional_path_has_source_and_inner_type() -> None:
    src, inner, reads_body, marker, fd, pipes = parse_extractor_hint(Optional[Path[int]])
    assert src == "path"
    assert inner is int
    assert marker is Path
    assert reads_body is False
    assert pipes == ()


def test_outer_optional_path_synthesises_none_default() -> None:
    _, _, _, _, fd, _ = parse_extractor_hint(Optional[Path[int]])
    assert isinstance(fd, FieldDescriptor)
    assert fd.default is None


def test_outer_optional_pep604_path_is_parsed_identically() -> None:
    src, inner, _, marker, fd, _ = parse_extractor_hint(Path[int] | None)
    assert src == "path"
    assert inner is int
    assert marker is Path
    assert fd is not None and fd.default is None


def test_outer_optional_query_is_parsed_identically() -> None:
    src, inner, _, marker, fd, _ = parse_extractor_hint(Optional[Query[str]])
    assert src == "query"
    assert inner is str
    assert marker is Query
    assert fd is not None and fd.default is None


def test_outer_optional_header_and_cookie_parse_correctly() -> None:
    for marker_cls, annotation in [
        (Header, Optional[Header[int]]),
        (Cookie, Cookie[str] | None),
    ]:
        src, _, _, mk, fd, _ = parse_extractor_hint(annotation)
        assert mk is marker_cls
        assert fd is not None and fd.default is None


# ---------------------------------------------------------------------------
# ``parse_extractor_hint`` \u2014 inner optional (``Path[Optional[T]]``)
# ---------------------------------------------------------------------------


def test_inner_optional_preserves_inner_union_shape() -> None:
    src, inner, _, marker, fd, _ = parse_extractor_hint(Path[Optional[int]])
    assert src == "path"
    assert marker is Path
    # The inner type is carried through verbatim so scalar coercion can
    # peel the Optional at extraction time.
    assert inner == Optional[int]
    # ``Path[Optional[int]]`` does not inject a FieldDescriptor \u2014 the
    # inner-union shape is handled by ``_coerce_scalar`` directly.
    assert fd is None


def test_inner_optional_pep604_preserves_shape() -> None:
    _, inner, _, _, _, _ = parse_extractor_hint(Path[int | None])
    assert inner == (int | None)


# ---------------------------------------------------------------------------
# Scalar coercion threads None correctly through Optional shapes
# ---------------------------------------------------------------------------


def test_coerce_scalar_none_input_short_circuits() -> None:
    assert _coerce_scalar(None, int) is None  # type: ignore[arg-type]


def test_coerce_scalar_on_optional_int_coerces_underlying_type() -> None:
    assert _coerce_scalar("42", Optional[int]) == 42


def test_coerce_scalar_on_pep604_optional_int_coerces_underlying_type() -> None:
    assert _coerce_scalar("3.14", float | None) == 3.14


def test_coerce_scalar_on_non_optional_union_tries_branches_in_order() -> None:
    # ``Union[int, str]`` \u2014 "abc" fails int, succeeds as str.
    assert _coerce_scalar("abc", Union[int, str]) == "abc"
    # "42" succeeds as int before str is attempted.
    assert _coerce_scalar("42", Union[int, str]) == 42


# ---------------------------------------------------------------------------
# Composability \u2014 Optional combined with other Annotated metadata
# ---------------------------------------------------------------------------


def test_outer_optional_with_fielddescriptor_keeps_user_constraints() -> None:
    from typing import Annotated

    from lauren.extractors import PathField

    ann = Optional[Annotated[Path[int], PathField(ge=1, le=100)]]
    src, inner, _, marker, fd, _ = parse_extractor_hint(ann)
    assert src == "path"
    assert inner is int
    assert marker is Path
    assert fd is not None
    # User constraints preserved; default was a no-op sentinel before
    # optional unwrapping, so the synthesised None default wins.
    assert fd.ge == 1
    assert fd.le == 100
    assert fd.default is None


def test_outer_optional_leaves_existing_non_sentinel_default_intact() -> None:
    from typing import Annotated

    from lauren.extractors import PathField

    ann = Optional[Annotated[Path[int], PathField(default=7)]]
    _, _, _, _, fd, _ = parse_extractor_hint(ann)
    # User explicitly wrote ``default=7``; the outer-optional wrapper
    # must not overwrite that to ``None`` \u2014 the user's intent was a
    # concrete default for missing values.
    assert fd is not None and fd.default == 7
