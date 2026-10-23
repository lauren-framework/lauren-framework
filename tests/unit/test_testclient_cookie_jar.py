"""Unit tests for :class:`lauren.testing.CookieJar` (RFC 6265 + browser rules)."""

from __future__ import annotations

import pytest

from lauren.testing import CookieJar, StoredCookie


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _jar(**kw) -> tuple[CookieJar, FakeClock]:
    clock = FakeClock()
    return CookieJar(clock=clock, **kw), clock


def _store(
    jar: CookieJar, *values: str, scheme: str = "http", host: str = "testserver", path: str = "/"
) -> None:
    jar.extract(list(values), scheme=scheme, host=host, path=path)


def _send(
    jar: CookieJar, *, scheme: str = "http", host: str = "testserver", path: str = "/", extra=None
) -> str | None:
    return jar.header_value(scheme=scheme, host=host, path=path, extra=extra)


# -- storage -------------------------------------------------------------------


def test_host_only_cookie_is_sent_back_to_its_host() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc")
    assert _send(jar) == "sid=abc"


def test_host_only_cookie_is_not_sent_to_a_subdomain() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc", host="example.com")
    assert _send(jar, host="api.example.com") is None


def test_domain_cookie_is_sent_to_subdomains() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc; Domain=example.com", host="www.example.com")
    assert _send(jar, host="api.example.com") == "sid=abc"


def test_domain_attribute_that_does_not_match_the_host_is_rejected() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc; Domain=other.com", host="example.com")
    assert len(jar) == 0


def test_domain_cookie_is_not_matched_for_an_ip_host() -> None:
    jar, _ = _jar()
    jar.set("sid", "abc", domain="0.0.1")
    assert _send(jar, host="127.0.0.1") is None


@pytest.mark.parametrize(
    ("request_path", "expected_path"),
    [("/", "/"), ("/login", "/"), ("/account/login", "/account"), ("relative", "/")],
)
def test_default_path_is_the_request_directory(request_path: str, expected_path: str) -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc", path=request_path)
    assert next(iter(jar)).path == expected_path


@pytest.mark.parametrize(
    ("request_path", "sent"),
    [("/account", True), ("/account/", True), ("/account/x", True), ("/accounts", False), ("/", False)],
)
def test_path_matching(request_path: str, sent: bool) -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc; Path=/account")
    assert (_send(jar, path=request_path) is not None) is sent


def test_longer_paths_are_sent_first() -> None:
    jar, _ = _jar()
    _store(jar, "a=1; Path=/", "b=2; Path=/x")
    assert _send(jar, path="/x/y") == "b=2; a=1"


def test_same_name_different_paths_are_distinct_cookies() -> None:
    jar, _ = _jar()
    _store(jar, "a=1; Path=/", "a=2; Path=/x")
    assert len(jar) == 2


def test_invalid_set_cookie_values_are_ignored() -> None:
    jar, _ = _jar()
    _store(jar, "novalue", "=nothing", "")
    assert len(jar) == 0


# -- expiry ----------------------------------------------------------------------


def test_max_age_expires_the_cookie() -> None:
    jar, clock = _jar()
    _store(jar, "sid=abc; Max-Age=60")
    clock.now += 61
    assert jar.get("sid") is None


def test_max_age_zero_deletes_an_existing_cookie() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc")
    _store(jar, "sid=; Max-Age=0")
    assert "sid" not in jar


def test_max_age_wins_over_expires() -> None:
    jar, clock = _jar()
    _store(jar, "sid=abc; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=60")
    assert jar.get("sid") == "abc"
    assert next(iter(jar)).expires == clock.now + 60


def test_expires_in_the_past_deletes_the_cookie() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc")
    _store(jar, "sid=gone; Expires=Thu, 01 Jan 1970 00:00:00 GMT")
    assert "sid" not in jar


def test_expires_in_the_future_is_kept() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc; Expires=Wed, 21 Oct 2099 07:28:00 GMT")
    assert jar.get("sid") == "abc"


def test_non_numeric_max_age_is_ignored() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc; Max-Age=soon")
    assert next(iter(jar)).expires is None


# -- Secure and prefixes ------------------------------------------------------------


@pytest.mark.parametrize("host", ["testserver", "localhost", "app.localhost", "127.0.0.1", "::1"])
def test_secure_cookie_round_trips_on_trustworthy_http_hosts(host: str) -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc; Secure", host=host)
    assert _send(jar, host=host) == "sid=abc"


def test_secure_cookie_from_an_untrusted_http_host_is_rejected() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc; Secure", host="example.com")
    assert len(jar) == 0


def test_secure_cookie_is_not_sent_over_plain_http_to_untrusted_hosts() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc; Secure", scheme="https", host="example.com")
    assert _send(jar, scheme="http", host="example.com") is None
    assert _send(jar, scheme="https", host="example.com") == "sid=abc"


def test_strict_cookies_need_https_for_secure() -> None:
    jar, _ = _jar(strict=True)
    _store(jar, "sid=abc; Secure")
    assert len(jar) == 0


def test_strict_cookies_still_accept_secure_over_https() -> None:
    jar, _ = _jar(strict=True)
    _store(jar, "sid=abc; Secure", scheme="https")
    assert _send(jar, scheme="https") == "sid=abc"


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("__Secure-sid=a; Secure", True),
        ("__Secure-sid=a", False),
        ("__Host-sid=a; Secure; Path=/", True),
        ("__Host-sid=a; Path=/", False),
        ("__Host-sid=a; Secure; Path=/x", False),
        ("__Host-sid=a; Secure; Path=/; Domain=testserver", False),
        ("__host-sid=a; Path=/", False),
    ],
)
def test_cookie_prefix_rules(value: str, stored: bool) -> None:
    jar, _ = _jar()
    _store(jar, value)
    assert (len(jar) == 1) is stored


def test_http_only_and_same_site_are_recorded() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc; HttpOnly; SameSite=Lax")
    cookie = next(iter(jar))
    assert (cookie.http_only, cookie.same_site) == (True, "Lax")


# -- per-request cookies and the mapping API -------------------------------------------


def test_per_request_cookies_override_without_persisting() -> None:
    jar, _ = _jar()
    _store(jar, "sid=abc", "theme=dark")
    assert _send(jar, extra={"sid": "override", "x": "1"}) == "theme=dark; sid=override; x=1"
    assert jar.get("sid") == "abc"
    assert "x" not in jar


def test_set_get_delete_clear() -> None:
    jar, _ = _jar()
    jar.set("theme", "dark", path="/")
    assert jar.get("theme") == "dark" and "theme" in jar
    jar.delete("theme")
    assert jar.get("theme", "none") == "none"
    jar.set("a", "1")
    jar.clear()
    assert len(jar) == 0


def test_get_filters_by_domain_and_path() -> None:
    jar, _ = _jar()
    jar.set("a", "root", path="/")
    jar.set("a", "deep", path="/x")
    assert jar.get("a", path="/x") == "deep"
    assert jar.get("a", domain="elsewhere") is None


def test_delete_can_target_one_path() -> None:
    jar, _ = _jar()
    jar.set("a", "root", path="/")
    jar.set("a", "deep", path="/x")
    jar.delete("a", path="/x")
    assert [c.path for c in jar] == ["/"]


def test_iteration_yields_stored_cookie_records() -> None:
    jar, _ = _jar()
    jar.set("a", "1")
    assert list(jar) == [StoredCookie(name="a", value="1", domain="testserver")]


def test_as_dict_and_repr() -> None:
    jar, _ = _jar()
    jar.set("a", "1")
    assert jar.as_dict() == {"a": "1"}
    assert "a" in repr(jar)


def test_contains_rejects_non_strings() -> None:
    jar, _ = _jar()
    jar.set("a", "1")
    assert 1 not in jar


def test_empty_value_cookie_is_present() -> None:
    jar, _ = _jar()
    _store(jar, "flag=")
    assert "flag" in jar
