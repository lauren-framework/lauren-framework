"""``@set_metadata`` follows the strict-inheritance rule on classes (F23 §1.3).

A subclass that declares its own ``@set_metadata`` must not absorb its
parent's keys; ``@propagate_metadata(Parent)`` remains the explicit opt-in.
"""

from __future__ import annotations

from lauren import MetadataSetKey, controller, propagate_metadata, set_metadata
from lauren.reflect import reflect_user_metadata


def test_subclass_with_its_own_metadata_does_not_absorb_parent_keys() -> None:
    @set_metadata("tenant", "acme")
    @controller("/base")
    class Base: ...

    @set_metadata("feature", "x")
    @controller("/child")
    class Child(Base): ...

    assert reflect_user_metadata(Child) == {"feature": "x"}
    assert reflect_user_metadata(Base) == {"tenant": "acme"}


def test_subclass_without_metadata_inherits_nothing() -> None:
    @set_metadata("tenant", "acme")
    @controller("/base")
    class Base: ...

    @controller("/child")
    class Child(Base): ...

    assert reflect_user_metadata(Child) == {}


def test_typed_keys_on_a_subclass_do_not_absorb_parent_items() -> None:
    roles = MetadataSetKey[str]("app.roles")

    @roles("admin")
    @controller("/base")
    class Base: ...

    @roles("auditor")
    @controller("/child")
    class Child(Base): ...

    assert reflect_user_metadata(Child, roles) == ("auditor",)


def test_propagate_metadata_still_opts_in_to_parent_keys() -> None:
    @set_metadata("tenant", "acme")
    @controller("/base")
    class Base: ...

    @propagate_metadata(Base)
    @set_metadata("feature", "x")
    @controller("/child")
    class Child(Base): ...

    assert reflect_user_metadata(Child) == {"tenant": "acme", "feature": "x"}


def test_propagate_metadata_target_scalar_wins_and_set_keys_union() -> None:
    roles = MetadataSetKey[str]("app.roles")

    @roles("admin")
    @set_metadata("tier", "gold")
    class Source: ...

    @propagate_metadata(Source)
    @roles("auditor")
    @set_metadata("tier", "silver")
    class Target: ...

    assert reflect_user_metadata(Target, roles) == ("admin", "auditor")
    assert reflect_user_metadata(Target, "tier") == "silver"


def test_propagate_metadata_copies_typed_keys_onto_a_bare_target() -> None:
    roles = MetadataSetKey[str]("app.roles")

    @roles("admin")
    def source() -> None: ...

    @propagate_metadata(source)
    def target() -> None: ...

    assert reflect_user_metadata(target, roles) == ("admin",)
    assert target.__lauren_metadata_keys__ == {"app.roles": roles}  # type: ignore[attr-defined]


def test_propagate_metadata_rejects_colliding_keys() -> None:
    import pytest

    from lauren import DecoratorUsageError

    @MetadataSetKey[str]("app.roles")("admin")
    def source() -> None: ...

    with pytest.raises(DecoratorUsageError):

        @propagate_metadata(source)
        @MetadataSetKey[str]("app.roles")("auditor")
        def target() -> None: ...
