"""The URL signer: canonical form, expiry, key rotation, tampering, bindings."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qsl

import pytest

from lauren._signing import Signer
from lauren._urls._signing import UrlSigner
from lauren.config import Secret
from lauren.exceptions import StartupError
from lauren.urls import UrlSigningConfig

NOW = 1_800_000_000.0


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _signer(clock: Clock | None = None, *, has_origin: bool = False, **config: object) -> UrlSigner:
    config.setdefault("secret", "k-new")
    return UrlSigner(UrlSigningConfig(clock=clock or Clock(), **config), has_origin=has_origin)  # type: ignore[arg-type]


def _sign(signer: UrlSigner, path: str = "/files/7", query: list | None = None, **kw: object) -> str:
    return signer.sign(
        method=str(kw.pop("method", "GET")),
        origin=str(kw.pop("origin", "")),
        path=path,
        query=query or [],
        expires_in=kw.pop("expires_in", None),
    )


def _verify(signer: UrlSigner, query: str, path: str = "/files/7", **kw: str) -> str | None:
    return signer.verify(method=kw.get("method", "GET"), origin=kw.get("origin", ""), path=path, query=query)


def test_signed_query_carries_exp_and_a_hex_signature() -> None:
    pairs = dict(parse_qsl(_sign(_signer(), expires_in=60)))

    assert pairs["exp"] == str(int(NOW) + 60)
    assert len(pairs["sig"]) == 64


def test_round_trip_verifies() -> None:
    signer = _signer()

    assert _verify(signer, _sign(signer)) is None


def test_default_ttl_applies_when_expires_in_is_omitted() -> None:
    pairs = dict(parse_qsl(_sign(_signer(default_ttl=120))))

    assert pairs["exp"] == str(int(NOW) + 120)


def test_timedelta_expiry_is_accepted() -> None:
    pairs = dict(parse_qsl(_sign(_signer(), expires_in=timedelta(minutes=2))))

    assert pairs["exp"] == str(int(NOW) + 120)


def test_query_order_does_not_matter() -> None:
    signer = _signer()
    signed = _sign(signer, query=[("b", "2"), ("a", "1")])
    exp_sig = "&".join(part for part in signed.split("&") if part.startswith(("exp=", "sig=")))

    assert _verify(signer, f"a=1&b=2&{exp_sig}") is None


def test_equivalent_query_encodings_verify_alike() -> None:
    signer = _signer()
    signed = _sign(signer, query=[("q", "a b")])

    assert "q=a%20b" in signed
    assert _verify(signer, signed.replace("q=a%20b", "q=a+b")) is None


def test_signed_list_values_repeat_the_key() -> None:
    signer = _signer()
    signed = _sign(signer, query=[("tag", ["x", "y"]), ("skip", None)])

    assert signed.startswith("tag=x&tag=y&exp=")
    assert _verify(signer, signed) is None


def test_expired_link_is_reported_as_expired() -> None:
    clock = Clock()
    signer = _signer(clock)
    signed = _sign(signer, expires_in=60)

    clock.now = NOW + 61

    assert _verify(signer, signed) == "signature_expired"


def test_link_is_valid_until_its_expiry_second() -> None:
    clock = Clock()
    signer = _signer(clock)
    signed = _sign(signer, expires_in=60)

    clock.now = NOW + 60

    assert _verify(signer, signed) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda q: q.replace("exp=", "exp=1"),
        lambda q: q.replace("sig=", "sig=0")[:-1],
        lambda q: q + "&extra=1",
        lambda q: q.replace("x=1", "x=2"),
    ],
)
def test_tampered_query_is_invalid(mutate) -> None:
    signer = _signer()

    assert _verify(signer, mutate(_sign(signer, query=[("x", "1")]))) == "signature_invalid"


def test_tampered_path_is_invalid() -> None:
    signer = _signer()

    assert _verify(signer, _sign(signer), path="/files/8") == "signature_invalid"


def test_path_is_compared_in_decoded_form() -> None:
    signer = _signer()

    assert _verify(signer, _sign(signer, path="/files/a%20b"), path="/files/a b") is None


def test_double_encoded_path_does_not_reuse_a_signature() -> None:
    signer = _signer()

    assert _verify(signer, _sign(signer, path="/files/a%20b"), path="/files/a%20b") == "signature_invalid"


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("", "signature_missing"),
        ("exp=1", "signature_missing"),
        ("sig=" + "0" * 64, "signature_missing"),
        ("exp=1&exp=2&sig=" + "0" * 64, "signature_invalid"),
        ("exp=-1&sig=" + "0" * 64, "signature_invalid"),
        ("exp=1&sig=xyz", "signature_invalid"),
        ("exp=1&sig=" + "A" * 64, "signature_invalid"),
    ],
)
def test_malformed_signature_parameters(query: str, reason: str) -> None:
    assert _verify(_signer(), query) == reason


def test_rotation_old_key_verifies_new_key_signs() -> None:
    old = _signer(secret="k-old")
    rotated = _signer(secret=["k-new", "k-old"])
    signed_with_old = _sign(old)

    assert _verify(rotated, signed_with_old) is None
    assert _verify(_signer(secret="k-new"), _sign(rotated)) is None
    assert _verify(old, _sign(rotated)) == "signature_invalid"


def test_secret_wrapper_is_unwrapped() -> None:
    signer = _signer(secret=Secret("k-new"))

    assert _verify(_signer(), _sign(signer)) is None


def test_url_signatures_do_not_verify_in_another_namespace() -> None:
    signer = _signer()
    pairs = dict(parse_qsl(_sign(signer)))
    message = "\n".join(("", "", "/files/7", "", pairs["exp"]))

    assert Signer((b"k-new",)).unsign(f"{message}.{pairs['sig']}") is None


def test_bind_method_rejects_another_method() -> None:
    signer = _signer(bind_method=True)
    signed = _sign(signer, method="GET")

    assert _verify(signer, signed, method="GET") is None
    assert _verify(signer, signed, method="DELETE") == "signature_invalid"


def test_without_bind_method_the_method_is_not_signed() -> None:
    signer = _signer()

    assert _verify(signer, _sign(signer, method="GET"), method="POST") is None


def test_bind_host_rejects_another_origin() -> None:
    signer = _signer(bind_host=True, has_origin=True)
    signed = _sign(signer, origin="https://api.example.com")

    assert _verify(signer, signed, origin="https://api.example.com") is None
    assert _verify(signer, signed, origin="https://evil.example") == "signature_invalid"


def test_reserved_query_names_cannot_be_signed() -> None:
    with pytest.raises(ValueError, match="reserved"):
        _sign(_signer(), query=[("sig", "x")])


@pytest.mark.parametrize("bad", [0, -5, float("inf"), True, "60"])
def test_invalid_expires_in_is_a_value_error(bad: object) -> None:
    with pytest.raises(ValueError, match="expires_in"):
        _sign(_signer(), expires_in=bad)


def test_custom_parameter_names() -> None:
    signer = _signer(expires_param="e", signature_param="s")
    signed = _sign(signer)

    assert set(dict(parse_qsl(signed))) == {"e", "s"}
    assert _verify(signer, signed) is None


@pytest.mark.parametrize(
    ("config", "field"),
    [
        ({"secret": ""}, "secret"),
        ({"secret": []}, "secret"),
        ({"secret": 42}, "secret"),
        ({"default_ttl": 0}, "default_ttl"),
        ({"bind_method": "yes"}, "bind_method"),
        ({"expires_param": ""}, "expires_param"),
        ({"signature_param": " sig"}, "signature_param"),
        ({"expires_param": "x", "signature_param": "x"}, "signature_param"),
        ({"clock": 5}, "clock"),
        ({"bind_host": True}, "bind_host"),
    ],
)
def test_invalid_signing_config_is_a_startup_error(config: dict, field: str) -> None:
    config.setdefault("secret", "k")
    with pytest.raises(StartupError) as info:
        UrlSigner(UrlSigningConfig(**config), has_origin=False)

    assert info.value.detail == {"reason": "invalid_url_signing_config", "field": field}
    assert "k-" not in str(info.value)


def test_non_config_value_is_a_startup_error() -> None:
    with pytest.raises(StartupError) as info:
        UrlSigner({"secret": "k"}, has_origin=False)

    assert info.value.detail["reason"] == "invalid_url_signing_config"


def test_secret_is_not_in_the_repr() -> None:
    assert "hunter2" not in repr(UrlSigningConfig(secret="hunter2"))
