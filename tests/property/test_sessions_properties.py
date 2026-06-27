"""Hypothesis property tests for session invariants.

Skipped automatically when hypothesis is not installed.
"""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import assume, given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

# JSON-compatible values (no NaN/Infinity so the round-trip is exact).
_json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**12), max_value=10**12)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text()
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=10), children, max_size=5)
    ),
    max_leaves=20,
)
_json_dicts = st.dictionaries(st.text(max_size=10), _json_values, max_size=8)


class TestSerializerProperties:
    @given(data=_json_dicts)
    def test_roundtrip(self, data):
        from lauren.sessions import JSONSessionSerializer

        ser = JSONSessionSerializer()
        assert ser.loads(ser.dumps(data)) == data


class TestSigningProperties:
    @given(value=st.text(min_size=1, max_size=200), secret=st.binary(min_size=1, max_size=64))
    def test_sign_unsign_roundtrip(self, value, secret):
        from lauren._sessions._signing import Signer

        signer = Signer((secret,))
        assert signer.unsign(signer.sign(value)) == value

    @given(
        value=st.text(min_size=1, max_size=80),
        secret=st.binary(min_size=1, max_size=32),
        index=st.integers(min_value=0, max_value=10_000),
    )
    def test_single_char_mutation_fails(self, value, secret, index):
        from lauren._sessions._signing import Signer

        signer = Signer((secret,))
        token = signer.sign(value)
        idx = index % len(token)
        replacement = "A" if token[idx] != "A" else "B"
        mutated = token[:idx] + replacement + token[idx + 1 :]
        assume(mutated != token)
        # A single-byte mutation can never validate (collision prob ~2**-256).
        assert signer.unsign(mutated) is None

    @given(secret=st.binary(min_size=1, max_size=32))
    def test_new_id_roundtrips_and_has_no_separator(self, secret):
        from lauren._sessions._signing import Signer
        from lauren._sessions._store import InMemorySessionStore

        store = InMemorySessionStore()
        signer = Signer((secret,))
        sid = store.new_id()
        assert "." not in sid
        assert signer.unsign(signer.sign(sid)) == sid


class TestSessionProperties:
    @given(data=_json_dicts)
    def test_session_never_crashes_on_arbitrary_payload(self, data):
        from lauren.sessions import Session

        session = Session()
        for key, value in data.items():
            session[key] = value
        # Reads + introspection must not crash and must reflect the writes.
        assert session.as_dict() == data
        for key in data:
            assert key in session
            _ = session.get(key)
        assert len(session) == len(data)
