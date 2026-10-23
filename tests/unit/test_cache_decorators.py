"""Unit tests for ``@cache_response`` / ``@idempotent`` and the response-cache helpers."""

from __future__ import annotations

from typing import Any

import pytest

from lauren import DecoratorUsageError, Headers, MutableHeaders, Response
from lauren._cache._policy import CACHE_RESPONSE_META, IDEMPOTENT_META
from lauren._cache._response import (
    client_requests_no_cache,
    decode_response,
    encode_response,
    is_storable,
    response_cache_key,
)
from lauren.cache import (
    DEFAULT_CACHEABLE_STATUSES,
    CachePolicy,
    IdempotencyPolicy,
    cache_response,
    idempotent,
)


async def handler() -> dict:  # pragma: no cover - only decorated
    return {}


def fresh() -> Any:
    async def fn() -> dict:  # pragma: no cover - only decorated
        return {}

    return fn


# ---------------------------------------------------------------------------
# @cache_response
# ---------------------------------------------------------------------------


def test_cache_response_stores_a_policy_on_the_same_function() -> None:
    fn = fresh()
    decorated = cache_response(ttl=30, vary=("Accept-Language", "accept"), tags=("catalog",))(fn)
    assert decorated is fn
    policy = getattr(fn, CACHE_RESPONSE_META)
    assert policy == CachePolicy(ttl=30.0, vary=("accept", "accept-language"), tags=("catalog",))
    assert policy.methods == frozenset({"GET", "HEAD"})
    assert policy.statuses == DEFAULT_CACHEABLE_STATUSES


def test_cache_response_rejects_bare_usage() -> None:
    with pytest.raises(DecoratorUsageError, match="must be used with parentheses"):
        cache_response(fresh())


def test_cache_response_rejects_positional_arguments() -> None:
    with pytest.raises(DecoratorUsageError, match="keyword arguments only"):
        cache_response(30)


@pytest.mark.parametrize("ttl", [None, 0, -1, True, "30", float("nan")])
def test_cache_response_requires_a_positive_ttl(ttl: Any) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        cache_response(ttl=ttl)
    assert info.value.detail["argument"] == "ttl"


@pytest.mark.parametrize("vary", [("",), ("*",), (3,)])
def test_cache_response_rejects_invalid_vary_entries(vary: Any) -> None:
    with pytest.raises(DecoratorUsageError, match="vary"):
        cache_response(ttl=1, vary=vary)


def test_cache_response_accepts_a_single_vary_string() -> None:
    fn = cache_response(ttl=1, vary="Accept-Language")(fresh())
    assert getattr(fn, CACHE_RESPONSE_META).vary == ("accept-language",)


@pytest.mark.parametrize("key", ["user", lambda: 1, lambda a, b: 1])
def test_cache_response_key_must_take_the_context(key: Any) -> None:
    with pytest.raises(DecoratorUsageError, match="key"):
        cache_response(ttl=1, key=key)


def test_cache_response_accepts_a_key_without_an_introspectable_signature() -> None:
    class Opaque:
        __signature__ = "not a signature"

        def __call__(self, ctx: Any) -> str:  # pragma: no cover - never called
            return "k"

    key_fn = Opaque()
    fn = cache_response(ttl=1, key=key_fn)(fresh())
    assert getattr(fn, CACHE_RESPONSE_META).key is key_fn


@pytest.mark.parametrize("tags", [("",), (1,)])
def test_cache_response_rejects_invalid_tags(tags: Any) -> None:
    with pytest.raises(DecoratorUsageError, match="tags"):
        cache_response(ttl=1, tags=tags)


def test_cache_response_accepts_tag_strings_and_callables() -> None:
    single = cache_response(ttl=1, tags="catalog")(fresh())
    assert getattr(single, CACHE_RESPONSE_META).tags == ("catalog",)
    dynamic = cache_response(ttl=1, tags=lambda ctx: ("p",))(fresh())
    assert callable(getattr(dynamic, CACHE_RESPONSE_META).tags)
    with pytest.raises(DecoratorUsageError, match="tags"):
        cache_response(ttl=1, tags=lambda: ("p",))


@pytest.mark.parametrize("methods", [("POST",), ("GET", "DELETE"), ()])
def test_cache_response_only_caches_safe_methods(methods: Any) -> None:
    with pytest.raises(DecoratorUsageError, match="subset of GET and HEAD"):
        cache_response(ttl=1, methods=methods)


def test_cache_response_methods_may_be_narrowed_to_get() -> None:
    fn = cache_response(ttl=1, methods="get")(fresh())
    assert getattr(fn, CACHE_RESPONSE_META).methods == frozenset({"GET"})


@pytest.mark.parametrize("statuses", [(99,), (600,), ("200",), (True,)])
def test_cache_response_rejects_invalid_statuses(statuses: Any) -> None:
    with pytest.raises(DecoratorUsageError, match="statuses"):
        cache_response(ttl=1, statuses=statuses)


def test_stacking_cache_response_is_rejected() -> None:
    fn = cache_response(ttl=1)(fresh())
    with pytest.raises(DecoratorUsageError) as info:
        cache_response(ttl=2)(fn)
    assert info.value.detail["reason"] == "already_cached"


def test_cache_response_and_idempotent_cannot_share_a_handler() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        cache_response(ttl=1)(idempotent()(fresh()))
    assert info.value.detail["reason"] == "already_idempotent"
    with pytest.raises(DecoratorUsageError) as info:
        idempotent()(cache_response(ttl=1)(fresh()))
    assert info.value.detail["reason"] == "already_cached"


def test_cache_response_rejects_classes_and_non_callables() -> None:
    with pytest.raises(DecoratorUsageError, match="not classes"):
        cache_response(ttl=1)(type("Ctrl", (), {}))
    with pytest.raises(DecoratorUsageError, match="must decorate a route handler"):
        cache_response(ttl=1)(42)


def test_cache_response_works_on_staticmethod_objects() -> None:
    target = staticmethod(fresh())
    assert cache_response(ttl=1)(target) is target
    assert getattr(target, CACHE_RESPONSE_META).ttl == 1.0


# ---------------------------------------------------------------------------
# @idempotent
# ---------------------------------------------------------------------------


def test_idempotent_defaults() -> None:
    fn = fresh()
    assert idempotent()(fn) is fn
    assert getattr(fn, IDEMPOTENT_META) == IdempotencyPolicy()


def test_idempotent_rejects_bare_and_positional_usage() -> None:
    with pytest.raises(DecoratorUsageError, match="parentheses"):
        idempotent(fresh())
    with pytest.raises(DecoratorUsageError, match="keyword arguments only"):
        idempotent(60)


@pytest.mark.parametrize(
    ("kwargs", "argument"),
    [
        ({"ttl": 0}, "ttl"),
        ({"lock_ttl": -1}, "lock_ttl"),
        ({"header": ""}, "header"),
        ({"header": "Idempotency Key"}, "header"),
        ({"header": 5}, "header"),
        ({"required": "yes"}, "required"),
        ({"scope": "user"}, "scope"),
    ],
)
def test_idempotent_validates_its_arguments(kwargs: dict[str, Any], argument: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        idempotent(**kwargs)
    assert info.value.detail["argument"] == argument


def test_stacking_idempotent_is_rejected() -> None:
    fn = idempotent()(fresh())
    with pytest.raises(DecoratorUsageError):
        idempotent()(fn)


# ---------------------------------------------------------------------------
# Keying
# ---------------------------------------------------------------------------


def key(**overrides: Any) -> str:
    args: dict[str, Any] = {
        "method": "GET",
        "route_template": "/items/{id}",
        "path_params": {"id": "1"},
        "query_params": {"a": ["1"], "b": ["2"]},
        "headers": Headers([("accept-language", "en")]),
        "vary": ("accept-language",),
        "custom": None,
    }
    args.update(overrides)
    return response_cache_key(**args)


def test_key_is_namespaced_by_route_template() -> None:
    assert key().startswith("resp:/items/{id}:")


def test_key_ignores_the_order_of_distinct_query_parameters() -> None:
    assert key(query_params={"b": ["2"], "a": ["1"]}) == key()


def test_key_keeps_the_order_of_repeated_values() -> None:
    assert key(query_params={"a": ["1", "2"]}) != key(query_params={"a": ["2", "1"]})


def test_head_shares_the_get_entry() -> None:
    assert key(method="HEAD") == key()


@pytest.mark.parametrize(
    "overrides",
    [
        {"path_params": {"id": "2"}},
        {"query_params": {"a": ["1"]}},
        {"headers": Headers([("accept-language", "fr")])},
        {"headers": Headers([])},
        {"custom": "user-1"},
        {"route_template": "/other"},
    ],
)
def test_key_changes_with_every_keyed_input(overrides: dict[str, Any]) -> None:
    assert key(**overrides) != key()


def test_headers_outside_vary_do_not_change_the_key() -> None:
    assert key(headers=Headers([("accept-language", "en"), ("user-agent", "x")])) == key()


# ---------------------------------------------------------------------------
# Store-time rules, client no-cache, encoding
# ---------------------------------------------------------------------------


POLICY = CachePolicy(ttl=10, vary=("accept-language",))


class _EmptyStream:
    """A streaming body (never iterated here)."""

    def __aiter__(self) -> _EmptyStream:
        return self

    async def __anext__(self) -> bytes:  # pragma: no cover - never iterated
        raise StopAsyncIteration


def response(status: int = 200, headers: list[tuple[str, str]] | None = None, **kwargs: Any) -> Response:
    return Response(b"body", status=status, headers=Headers(headers or []), **kwargs)


@pytest.mark.parametrize("status", sorted(DEFAULT_CACHEABLE_STATUSES))
def test_default_cacheable_statuses_are_stored(status: int) -> None:
    assert is_storable(response(status), POLICY)


@pytest.mark.parametrize(
    "candidate",
    [
        response(201),
        response(500),
        response(headers=[("set-cookie", "a=b")]),
        response(headers=[("cache-control", "private, max-age=60")]),
        response(headers=[("cache-control", "no-store")]),
        response(headers=[("cache-control", "max-age=0, no-cache")]),
        response(headers=[("vary", "*")]),
        response(headers=[("vary", "Accept-Language, Cookie")]),
        Response.stream(_EmptyStream()),
    ],
)
def test_unsafe_responses_are_not_stored(candidate: Response) -> None:
    assert not is_storable(candidate, POLICY)


def test_vary_headers_covered_by_the_key_are_stored() -> None:
    assert is_storable(response(headers=[("vary", "Accept-Language"), ("cache-control", "public")]), POLICY)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([("cache-control", "no-cache")], True),
        ([("cache-control", "max-age=0, No-Cache")], True),
        ([("pragma", "no-cache")], True),
        ([("cache-control", "max-age=0")], False),
        ([], False),
    ],
)
def test_client_no_cache_detection(headers: list[tuple[str, str]], expected: bool) -> None:
    assert client_requests_no_cache(Headers(headers)) is expected


def test_encoding_drops_hop_by_hop_and_volatile_headers() -> None:
    original = Response(
        b"payload\nwith newline",
        status=203,
        headers=MutableHeaders(
            [
                ("content-type", "application/json"),
                ("etag", '"v1"'),
                ("connection", "close"),
                ("transfer-encoding", "chunked"),
                ("content-length", "3"),
                ("age", "5"),
                ("x-cache", "MISS"),
            ]
        ),
    )
    rebuilt, header = decode_response(encode_response(original, extra={"f": "fp"}))
    assert rebuilt.status == 203
    assert rebuilt.body == b"payload\nwith newline"
    assert rebuilt.headers.raw() == [("content-type", "application/json"), ("etag", '"v1"')]
    assert header["f"] == "fp"
