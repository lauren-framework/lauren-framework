"""The shared HMAC signer (``lauren._signing``) used by sessions and pagination cursors."""

from __future__ import annotations

import hmac
from hashlib import sha256

import pytest

from lauren._sessions import _signing as session_signing
from lauren._signing import Signer, normalize_secrets


def test_sessions_reuse_the_shared_signer() -> None:
    assert session_signing.Signer is Signer
    assert session_signing.normalize_secrets is normalize_secrets


def test_empty_namespace_keeps_the_session_cookie_format() -> None:
    token = Signer((b"k",)).sign("value")

    assert token == "value." + hmac.new(b"k", b"value", sha256).hexdigest()


def test_namespaced_token_round_trips() -> None:
    signer = Signer((b"k",), namespace="lauren.cursor")

    assert signer.unsign(signer.sign("value")) == "value"


@pytest.mark.parametrize(("signing", "verifying"), [("", "lauren.cursor"), ("lauren.cursor", ""), ("a", "b")])
def test_namespaces_never_verify_each_other(signing: str, verifying: str) -> None:
    token = Signer((b"k",), namespace=signing).sign("value")

    assert Signer((b"k",), namespace=verifying).unsign(token) is None


def test_rotation_verifies_with_every_secret() -> None:
    token = Signer((b"old",), namespace="n").sign("value")

    assert Signer((b"new", b"old"), namespace="n").unsign(token) == "value"


def test_signer_requires_a_secret() -> None:
    with pytest.raises(ValueError):
        Signer(())
