"""Property tests for the TestClient cookie jar: set-then-send and delete invariants."""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren.testing import CookieJar  # noqa: E402

# RFC 6265 cookie-name tokens and cookie-octet values (no separators / quotes / spaces).
_TOKEN = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'*+-.^_`|~",
    min_size=1,
    max_size=12,
).filter(lambda s: not s.lower().startswith(("__secure-", "__host-")))
_VALUE = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789!#$%&'()*+-./:<=>?@[]^_`{|}~", max_size=20)
_SEGMENT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=6)
_PATH = st.lists(_SEGMENT, max_size=3).map(lambda parts: "/" + "/".join(parts))


def _send(jar: CookieJar, path: str) -> dict[str, str]:
    header = jar.header_value(scheme="http", host="testserver", path=path)
    if header is None:
        return {}
    return dict(pair.split("=", 1) for pair in header.split("; "))


@settings(max_examples=150, deadline=None)
@given(name=_TOKEN, value=_VALUE, path=_PATH)
def test_a_stored_cookie_is_sent_back_on_its_path(name: str, value: str, path: str) -> None:
    jar = CookieJar()
    jar.extract([f"{name}={value}; Path={path}"], scheme="http", host="testserver", path=path)
    assert _send(jar, path) == {name: value}


@settings(max_examples=150, deadline=None)
@given(name=_TOKEN, value=_VALUE, path=_PATH)
def test_storing_twice_is_idempotent(name: str, value: str, path: str) -> None:
    jar = CookieJar()
    for _ in range(2):
        jar.extract([f"{name}={value}; Path={path}"], scheme="http", host="testserver", path=path)
    assert len(jar) == 1
    assert _send(jar, path) == {name: value}


@settings(max_examples=150, deadline=None)
@given(name=_TOKEN, value=_VALUE, path=_PATH)
def test_max_age_zero_always_removes(name: str, value: str, path: str) -> None:
    jar = CookieJar()
    jar.extract([f"{name}={value}; Path={path}"], scheme="http", host="testserver", path=path)
    jar.extract([f"{name}=; Path={path}; Max-Age=0"], scheme="http", host="testserver", path=path)
    assert name not in jar
    assert _send(jar, path) == {}


@settings(max_examples=150, deadline=None)
@given(cookies=st.dictionaries(_TOKEN, _VALUE, min_size=1, max_size=5))
def test_delete_removes_only_the_named_cookie(cookies: dict[str, str]) -> None:
    jar = CookieJar()
    for name, value in cookies.items():
        jar.set(name, value)
    victim = next(iter(cookies))
    jar.delete(victim)
    expected = {k: v for k, v in cookies.items() if k != victim}
    assert jar.as_dict() == expected
