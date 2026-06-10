"""Unit tests for the @propagate_metadata decorator."""

from __future__ import annotations


from lauren import (
    Scope,
    controller,
    exception_handler,
    get,
    injectable,
    propagate_metadata,
    set_metadata,
    use_encoder,
    use_exception_handlers,
    use_guards,
    use_interceptors,
    use_middlewares,
)
from lauren.reflect import (
    reflect_encoder,
    reflect_exception_handlers,
    reflect_guards,
    reflect_interceptors,
    reflect_middlewares,
    reflect_user_metadata,
)
from lauren.serialization import StdlibJSONEncoder


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class GuardA:
    async def can_activate(self, ctx):
        return True


@injectable(scope=Scope.SINGLETON)
class GuardB:
    async def can_activate(self, ctx):
        return True


@injectable(scope=Scope.SINGLETON)
class InterceptorA:
    async def intercept(self, ctx, ch):
        return await ch.handle()


@injectable(scope=Scope.SINGLETON)
class MiddlewareA:
    async def dispatch(self, req, call_next):
        return await call_next(req)


@exception_handler(ValueError)
class ValErrHandler:
    async def catch(self, exc, req):
        from lauren.types import Response

        return Response.json({"e": str(exc)}, status=400)


# ---------------------------------------------------------------------------
# guards propagation
# ---------------------------------------------------------------------------


class TestPropagateGuards:
    def test_copies_guards_from_source_to_empty_target(self):
        @use_guards(GuardA)
        class Source:
            pass

        @propagate_metadata(Source)
        @controller("/x")
        class Target:
            pass

        assert GuardA in reflect_guards(Target)

    def test_prepends_source_guards_before_existing(self):
        @use_guards(GuardA)
        class Source:
            pass

        @propagate_metadata(Source)
        @use_guards(GuardB)
        @controller("/x")
        class Target:
            pass

        guards = reflect_guards(Target)
        assert guards.index(GuardA) < guards.index(GuardB)

    def test_guards_false_skips_propagation(self):
        @use_guards(GuardA)
        class Source:
            pass

        @propagate_metadata(Source, guards=False)
        @controller("/x")
        class Target:
            pass

        assert GuardA not in reflect_guards(Target)

    def test_no_guards_on_source_is_noop(self):
        class Source:
            pass

        @use_guards(GuardB)
        @propagate_metadata(Source)
        @controller("/x")
        class Target:
            pass

        assert reflect_guards(Target) == (GuardB,)

    def test_propagates_to_function(self):
        @use_guards(GuardA)
        class Source:
            pass

        @propagate_metadata(Source)
        @get("/fn")
        async def handler():
            pass

        assert GuardA in reflect_guards(handler)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# interceptors propagation
# ---------------------------------------------------------------------------


class TestPropagateInterceptors:
    def test_copies_interceptors(self):
        @use_interceptors(InterceptorA)
        class Source:
            pass

        @propagate_metadata(Source)
        @controller("/x")
        class Target:
            pass

        assert InterceptorA in reflect_interceptors(Target)

    def test_interceptors_false_skips(self):
        @use_interceptors(InterceptorA)
        class Source:
            pass

        @propagate_metadata(Source, interceptors=False)
        @controller("/x")
        class Target:
            pass

        assert reflect_interceptors(Target) == ()


# ---------------------------------------------------------------------------
# middlewares propagation
# ---------------------------------------------------------------------------


class TestPropagateMiddlewares:
    def test_copies_middlewares(self):
        @use_middlewares(MiddlewareA)
        class Source:
            pass

        @propagate_metadata(Source)
        @controller("/x")
        class Target:
            pass

        assert MiddlewareA in reflect_middlewares(Target)

    def test_middlewares_false_skips(self):
        @use_middlewares(MiddlewareA)
        class Source:
            pass

        @propagate_metadata(Source, middlewares=False)
        @controller("/x")
        class Target:
            pass

        assert reflect_middlewares(Target) == ()


# ---------------------------------------------------------------------------
# exception_handlers propagation
# ---------------------------------------------------------------------------


class TestPropagateExceptionHandlers:
    def test_copies_exception_handlers(self):
        @use_exception_handlers(ValErrHandler)
        class Source:
            pass

        @propagate_metadata(Source)
        @controller("/x")
        class Target:
            pass

        assert ValErrHandler in reflect_exception_handlers(Target)

    def test_exception_handlers_false_skips(self):
        @use_exception_handlers(ValErrHandler)
        class Source:
            pass

        @propagate_metadata(Source, exception_handlers=False)
        @controller("/x")
        class Target:
            pass

        assert reflect_exception_handlers(Target) == ()


# ---------------------------------------------------------------------------
# encoder propagation
# ---------------------------------------------------------------------------


class TestPropagateEncoder:
    def test_copies_encoder_when_target_has_none(self):
        enc = StdlibJSONEncoder()

        @use_encoder(enc)
        @controller("/src")
        class Source:
            pass

        @propagate_metadata(Source)
        @controller("/x")
        class Target:
            pass

        assert reflect_encoder(Target) is enc

    def test_does_not_overwrite_target_encoder(self):
        enc_src = StdlibJSONEncoder()
        enc_tgt = StdlibJSONEncoder()

        @use_encoder(enc_src)
        @controller("/src")
        class Source:
            pass

        @propagate_metadata(Source)
        @use_encoder(enc_tgt)
        @controller("/x")
        class Target:
            pass

        assert reflect_encoder(Target) is enc_tgt

    def test_encoder_false_skips(self):
        enc = StdlibJSONEncoder()

        @use_encoder(enc)
        @controller("/src")
        class Source:
            pass

        @propagate_metadata(Source, encoder=False)
        @controller("/x")
        class Target:
            pass

        assert reflect_encoder(Target) is None


# ---------------------------------------------------------------------------
# user_metadata propagation
# ---------------------------------------------------------------------------


class TestPropagateUserMetadata:
    def test_copies_user_metadata(self):
        @set_metadata("role", "admin")
        @controller("/src")
        class Source:
            pass

        @propagate_metadata(Source)
        @controller("/x")
        class Target:
            pass

        assert reflect_user_metadata(Target, "role") == "admin"

    def test_target_keys_win_on_conflict(self):
        @set_metadata("role", "admin")
        @controller("/src")
        class Source:
            pass

        @propagate_metadata(Source)
        @set_metadata("role", "superuser")
        @controller("/x")
        class Target:
            pass

        assert reflect_user_metadata(Target, "role") == "superuser"

    def test_user_metadata_false_skips(self):
        @set_metadata("key", "val")
        @controller("/src")
        class Source:
            pass

        @propagate_metadata(Source, user_metadata=False)
        @controller("/x")
        class Target:
            pass

        assert reflect_user_metadata(Target, "key") is None

    def test_merge_disjoint_keys(self):
        @set_metadata("from_src", True)
        @controller("/src")
        class Source:
            pass

        @propagate_metadata(Source)
        @set_metadata("from_tgt", True)
        @controller("/x")
        class Target:
            pass

        meta = reflect_user_metadata(Target)
        assert meta["from_src"] is True
        assert meta["from_tgt"] is True


# ---------------------------------------------------------------------------
# all-disabled is a no-op
# ---------------------------------------------------------------------------


class TestPropagateAllDisabled:
    def test_no_op_when_all_false(self):
        @use_guards(GuardA)
        @set_metadata("k", "v")
        @controller("/src")
        class Source:
            pass

        @propagate_metadata(
            Source,
            guards=False,
            interceptors=False,
            middlewares=False,
            exception_handlers=False,
            encoder=False,
            user_metadata=False,
        )
        @controller("/x")
        class Target:
            pass

        assert reflect_guards(Target) == ()
        assert reflect_user_metadata(Target) == {}


# ---------------------------------------------------------------------------
# source from function
# ---------------------------------------------------------------------------


class TestPropagateFromFunction:
    def test_source_can_be_function(self):
        @use_guards(GuardA)
        @set_metadata("scope", "public")
        @get("/src")
        async def src_fn():
            pass

        @propagate_metadata(src_fn)
        @controller("/x")
        class Target:
            pass

        assert GuardA in reflect_guards(Target)
        assert reflect_user_metadata(Target, "scope") == "public"


# ---------------------------------------------------------------------------
# target isolation (own-dict rule preserved)
# ---------------------------------------------------------------------------


class TestPropagateIsolation:
    def test_propagation_does_not_affect_source(self):
        @use_guards(GuardA)
        class Source:
            pass

        @propagate_metadata(Source)
        @use_guards(GuardB)
        @controller("/x")
        class Target:
            pass

        # Source should still only have GuardA
        assert reflect_guards(Source) == (GuardA,)

    def test_two_targets_from_same_source_are_independent(self):
        @use_guards(GuardA)
        class Source:
            pass

        @propagate_metadata(Source)
        @controller("/x")
        class TargetX:
            pass

        @propagate_metadata(Source)
        @use_guards(GuardB)
        @controller("/y")
        class TargetY:
            pass

        assert reflect_guards(TargetX) == (GuardA,)
        assert reflect_guards(TargetY) == (GuardA, GuardB)
