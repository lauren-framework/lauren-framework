"""Manifest naming, metadata redaction and deterministic serialization."""

from __future__ import annotations

import json
from typing import Annotated

from lauren import Token
from lauren.config import Secret
from lauren.reflect._manifest import _metadata_key, _name, _safe_value


def test_token_names_do_not_depend_on_unique_token_identity() -> None:
    first, second = Token("database"), Token("database")

    assert first is not second
    assert _name(first) == _name(second) == "Token(database)"
    assert _name("database") == "'database'"


def test_type_names_are_readable_without_object_addresses() -> None:
    class Local:
        pass

    assert _name(list[int]) == "list[int]"
    assert _name(Local | None).endswith("Local | None")
    assert "0x" not in _name(Local())
    assert _name(Annotated[int, Secret("private")]) == "typing.Annotated[int, lauren._config._secret.Secret]"


def test_explicit_metadata_value_snapshot_redacts_nested_secrets() -> None:
    value = {
        "token": Secret("never-publish"),
        "items": [1, Secret("also-private")],
        "set": {2, 1},
    }

    result = _safe_value(value)

    assert result == {"items": [1, "***"], "set": [1, 2], "token": "***"}
    assert "never-publish" not in json.dumps(result)


def test_metadata_key_prefers_name_over_object_repr() -> None:
    class Key:
        name = "roles"

    assert _metadata_key("public") == "public"
    assert _metadata_key(Key()) == "roles"


def test_forward_refs_literals_and_unknown_metadata_are_stable() -> None:
    from typing import Any, ForwardRef, Literal

    assert _name(ForwardRef("User")) == "User"
    assert _name(Literal[1, 2]) == "Literal[1, 2]"
    assert _name(None) == "None"
    assert _name(Any) == "Any"
    assert _safe_value(object()) == "object"
    assert _metadata_key(object()) == "object"
