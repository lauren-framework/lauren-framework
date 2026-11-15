"""Property tests for the router (F11).

* **Differential** — for route sets without converters, the positional-capture
  matcher returns the same outcome as the pre-F11 router (a frozen copy below):
  same template and params, or the same miss / 405 (whose ``Allow`` now also
  lists ``HEAD`` next to ``GET``). The differential uses one declared method:
  mixed-method branches intentionally backtrack on a method miss now.
* **Converters** — ``matches(to_url(v))`` and ``to_python(to_url(v)) == v``.
* **Raw paths** — percent-encoding segments and routing with
  ``match_raw_path=True`` gives back the original segments.
"""

from __future__ import annotations

import math
import uuid
from typing import Any
from urllib.parse import quote

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren import RoutingConfig  # noqa: E402
from lauren._routing import Router  # noqa: E402
from lauren._routing._converters import build_converter  # noqa: E402
from lauren.exceptions import MethodNotAllowedError, RouteNotFoundError, RouterConflictError  # noqa: E402

# ---------------------------------------------------------------------------
# A frozen copy of the pre-F11 matcher (``lauren/_routing/__init__.py`` on dev)
# ---------------------------------------------------------------------------


class _OldNode:
    def __init__(self, segment: str = "") -> None:
        self.segment = segment
        self.children_static: dict[str, _OldNode] = {}
        self.child_param: _OldNode | None = None
        self.child_wildcard: _OldNode | None = None
        self.handlers: dict[str, str] = {}


def _old_split(path: str) -> list[str]:
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return [] if path == "/" else path[1:].split("/")


class _OldRouter:
    def __init__(self) -> None:
        self.root = _OldNode()
        self.static: dict[str, _OldNode] = {}
        self.has_dynamic = False

    def add(self, method: str, path: str) -> None:
        node = self.root
        static = True
        for seg in _old_split(path):
            if seg.startswith("{*"):
                static = False
                if node.child_wildcard is None:
                    node.child_wildcard = _OldNode(seg[2:-1])
                node = node.child_wildcard
            elif seg.startswith("{"):
                static = False
                if node.child_param is None:
                    node.child_param = _OldNode(seg[1:-1])
                elif node.child_param.segment != seg[1:-1]:
                    raise RouterConflictError("Parameter name conflict")
                node = node.child_param
            else:
                node = node.children_static.setdefault(seg, _OldNode(seg))
        if method in node.handlers:
            raise RouterConflictError("duplicate")
        node.handlers[method] = path
        if static:
            self.static[path] = node
        else:
            self.has_dynamic = True

    def _match(
        self, node: _OldNode, segs: list[str], i: int, params: dict[str, str], skip: str | None = None
    ) -> Any:
        if i == len(segs):
            if node.handlers:
                return node, params
            if node.child_wildcard is not None:
                return node.child_wildcard, {**params, node.child_wildcard.segment: ""}
            return None, params
        seg = segs[i]
        static = node.children_static.get(seg)
        if static is not None and not (i == 0 and seg == skip):
            found, p = self._match(static, segs, i + 1, params)
            if found is not None:
                return found, p
        if node.child_param is not None:
            found, p = self._match(node.child_param, segs, i + 1, {**params, node.child_param.segment: seg})
            if found is not None:
                return found, p
        if node.child_wildcard is not None:
            return node.child_wildcard, {**params, node.child_wildcard.segment: "/".join(segs[i:])}
        return None, params

    def find(self, method: str, path: str) -> tuple[str, Any]:
        segs = _old_split(path)
        key = "/" + "/".join(segs) if segs else "/"
        static = self.static.get(key)
        if static is not None:
            if method in static.handlers:
                return "ok", (static.handlers[method], {})
            if self.has_dynamic:
                node, params = self._match(self.root, segs, 0, {}, skip=segs[0] if segs else None)
                if node is not None and method in node.handlers:
                    return "ok", (node.handlers[method], params)
            return "405", sorted(static.handlers)
        node, params = self._match(self.root, segs, 0, {})
        if node is None:
            return "404", None
        if method not in node.handlers:
            return "405", sorted(node.handlers)
        return "ok", (node.handlers[method], params)


# ---------------------------------------------------------------------------
# Differential property
# ---------------------------------------------------------------------------

_STATIC = st.sampled_from(["a", "b", "users", "me", "x"])
_METHODS = st.sampled_from(["GET", "POST", "DELETE"])


@st.composite
def _route(draw: Any) -> tuple[str, str]:
    depth = draw(st.integers(min_value=0, max_value=4))
    segments: list[str] = []
    for level in range(depth):
        kind = draw(st.sampled_from(["static", "static", "param"]))
        # One parameter name per depth keeps the old router conflict-free.
        segments.append(draw(_STATIC) if kind == "static" else "{p" + str(level) + "}")
    if draw(st.booleans()) and depth < 4:
        segments.append("{*rest}")
    return "GET", "/" + "/".join(segments)


_REQUEST = st.lists(st.sampled_from(["a", "b", "users", "me", "x", "42"]), max_size=6).map(
    lambda segs: "/" + "/".join(segs)
)


@settings(max_examples=150, deadline=None)
@given(
    routes=st.lists(_route(), min_size=1, max_size=12),
    requests=st.lists(st.tuples(_METHODS, _REQUEST), max_size=10),
)
def test_positional_matcher_agrees_with_the_old_router(
    routes: list[tuple[str, str]], requests: list[tuple[str, str]]
) -> None:
    old, new = _OldRouter(), Router(RoutingConfig())
    for method, path in routes:
        try:
            old.add(method, path)
        except RouterConflictError:
            continue
        new.add_route(method, path, lambda: None)
    new.freeze()

    for method, path in requests:
        outcome, value = old.find(method, path)
        if outcome == "ok":
            entry, params = new.find(method, path)
            template, old_params = value
            assert entry.path_template == template.rstrip("/") or entry.path_template == "/"
            assert params == old_params
        elif outcome == "404":
            with pytest.raises(RouteNotFoundError):
                new.find(method, path)
        else:
            with pytest.raises(MethodNotAllowedError) as exc_info:
                new.find(method, path)
            assert [m for m in exc_info.value.allow if m != "HEAD"] == value


# ---------------------------------------------------------------------------
# Converter round trips (the F10 url_for seam)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "values"),
    [
        ("int", st.integers(min_value=0, max_value=10**30)),
        ("int(min=5, max=500)", st.integers(min_value=5, max_value=500)),
        ("sint", st.integers(min_value=-(10**30), max_value=10**30)),
        ("float", st.floats(allow_nan=False, allow_infinity=False)),
        ("uuid", st.uuids()),
        ("slug", st.from_regex(r"[a-z0-9]{1,8}(?:-[a-z0-9]{1,8}){0,3}", fullmatch=True)),
        ("re([A-Z]{2}[0-9]{3})", st.from_regex(r"[A-Z]{2}[0-9]{3}", fullmatch=True)),
        ("str", st.text(min_size=1)),
    ],
)
def test_converter_round_trip(spec: str, values: Any) -> None:
    converter = build_converter(spec, None)
    assert converter is not None

    @settings(max_examples=100, deadline=None)
    @given(value=values)
    def check(value: Any) -> None:
        text = converter.to_url(value)
        assert converter.matches(text)
        back = converter.to_python(text)
        if isinstance(value, float):
            assert math.isclose(back, value, rel_tol=0.0, abs_tol=0.0) or back == value
        elif isinstance(value, uuid.UUID):
            assert back == value
        else:
            assert back == value

    check()


# ---------------------------------------------------------------------------
# Raw paths: encode → route → decoded params
# ---------------------------------------------------------------------------

_SEGMENT_TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=12)


@settings(max_examples=150, deadline=None)
@given(first=_SEGMENT_TEXT, second=_SEGMENT_TEXT)
def test_raw_path_segments_round_trip(first: str, second: str) -> None:
    router = Router(RoutingConfig(match_raw_path=True))
    entry = router.add_route("GET", "/a/{x}/{y}", lambda: None)
    router.freeze()

    path = entry.build_path(x=first, y=second)
    found, params = router.find("GET", path)

    assert found is entry
    assert params == {"x": first, "y": second}
    assert path == "/a/" + quote(first, safe=":@!$&'()*+,;=") + "/" + quote(second, safe=":@!$&'()*+,;=")
