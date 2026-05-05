"""Custom provider helpers — :func:`use_value`, :func:`use_class`,
:func:`use_factory`, :func:`use_existing`, plus :class:`Token` and
:func:`Inject`.

NestJS exposes four "custom provider" recipes that go beyond the
standard ``@injectable`` class registration:

* ``useValue`` — bind a token to a literal value (mocks, constants,
  externally-constructed singletons);
* ``useClass`` — bind a token to a *different* class than the token
  itself, useful for environment-conditional swaps and test doubles;
* ``useFactory`` — compute the bound value from a function whose own
  parameters are resolved through DI;
* ``useExisting`` — alias one token to another so two names point at
  the same instance.

Lauren mirrors all four. The user-facing helpers in this module
return a :class:`CustomProvider` record; the factory pipeline in
:mod:`lauren._asgi` translates each record into the right
:class:`Provider` row inside the container at startup. Keeping the
record immutable means a module's ``providers=`` list can be built
once at import time and reused across multiple ``LaurenFactory.create``
calls (handy for tests that boot variations of the same graph).

The non-class token surface (string and :class:`Token` IDs) makes it
possible to register dependencies that don't have an obvious class
to attach to — database connections, raw config dicts, third-party
client instances. Inject them via ``param: Annotated[T, Inject("X")]``
or, when you'd rather keep the class form, by listing the same token
in a custom provider's ``injects=`` list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..exceptions import (
    DecoratorUsageError,
)
from ..types import Scope


# ---------------------------------------------------------------------------
# Token — opaque, hashable, debug-friendly identifier for non-class providers
# ---------------------------------------------------------------------------


class Token:
    """A typed, branded identifier for non-class providers.

    Plain strings already work as DI tokens, but ``Token("DB_URL")``
    gives three benefits over a bare string:

    * **Identity vs equality.** Two ``Token("X")`` instances are
      *different* tokens by default, mirroring Python's ``object``
      identity semantics. This is what NestJS's :class:`Symbol`-based
      tokens give you and what most Python users expect when they
      "create a new token". (Pass ``unique=False`` to opt into
      string-style equality if you need to share the same name across
      processes / modules.)
    * **Repr.** Errors mentioning ``Token("DB_URL")`` are far easier
      to grep for than errors mentioning a bare ``"DB_URL"``, which
      could be any string anywhere in the codebase.
    * **IDE friendliness.** A module-level ``DB_URL = Token("DB_URL")``
      gives autocomplete and "find usages" without making the token's
      name part of the public API surface.

    Tokens are hashable and compare by identity by default, so they
    are safe to use as dict keys and set members. They are *not*
    classes — passing a Token to a place that expects a Python type
    (like a function annotation) is fine because lauren's DI machinery
    treats tokens as opaque hashable keys, not as type objects.
    """

    __slots__ = ("_name", "_unique", "__weakref__")

    def __init__(self, name: str, *, unique: bool = True) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"Token(name) requires a non-empty string name; got {name!r}."
            )
        self._name = name
        self._unique = unique

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique(self) -> bool:
        return self._unique

    def __hash__(self) -> int:
        # Identity-hash for unique tokens so two ``Token("X")`` are
        # different keys; string-hash otherwise so cross-module token
        # sharing by name still works for users who explicitly opted
        # in via ``unique=False``.
        if self._unique:
            return object.__hash__(self)
        return hash(("lauren-token", self._name))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Token):
            return NotImplemented
        if self._unique or other._unique:
            return self is other
        return self._name == other._name

    def __repr__(self) -> str:
        kind = "unique" if self._unique else "shared"
        return f"Token({self._name!r}, {kind})"

    # ``__name__`` lets _describe() and the OpenAPI emitter render
    # tokens consistently with classes.
    @property
    def __name__(self) -> str:  # type: ignore[override]
        return f"Token({self._name})"


# ---------------------------------------------------------------------------
# Inject — annotation marker overriding the type-hint-as-token convention
# ---------------------------------------------------------------------------


class _InjectMarker:
    """Marker stored inside an ``Annotated[...]`` payload.

    Created by :func:`Inject`. The DI dep-collector recognises it and
    uses :attr:`token` as the resolution key instead of the bare
    annotation type. End users never construct one directly — they
    write ``Annotated[Conn, Inject("CONN")]`` and let the helper
    factory build the marker.
    """

    __slots__ = ("token",)

    def __init__(self, token: Any) -> None:
        self.token = token

    def __repr__(self) -> str:  # pragma: no cover - cosmetic only
        return f"Inject({self.token!r})"


def Inject(token: Any) -> _InjectMarker:
    """Override the resolution token for a parameter or class field.

    By default lauren resolves a parameter by its type annotation —
    ``def __init__(self, repo: UserRepo)`` looks up the ``UserRepo``
    provider. When the provider is registered against a non-class
    token (a string or a :class:`Token`), the type hint cannot itself
    *be* the token — Python wouldn't accept a string in that position.
    :func:`Inject` is the escape hatch::

        @injectable()
        class CatsRepository:
            def __init__(
                self,
                connection: Annotated[Connection, Inject("CONNECTION")],
            ) -> None:
                self.connection = connection

    The annotation type is still used by static checkers (mypy /
    pyright will still verify ``connection.execute(...)`` against the
    ``Connection`` interface), but the runtime resolution uses
    ``"CONNECTION"`` as the lookup key.

    Class fields work the same way::

        @injectable()
        class CatsRepository:
            connection: Annotated[Connection, Inject("CONNECTION")]

    For convenience inside ``inject=[...]`` factory lists you may also
    pass a bare token (string, :class:`Token`, class) directly without
    wrapping it in :func:`Inject` — the wrapper is only needed when
    you're writing a Python annotation.
    """
    return _InjectMarker(token)


# ---------------------------------------------------------------------------
# CustomProvider — normalised record produced by the user-facing helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomProvider:
    """Immutable description of a non-standard provider registration.

    Each variant of NestJS's "custom provider" recipe lowers into this
    single record. Module compilation reads the ``kind`` discriminator
    and dispatches to the right :class:`DIContainer` registration
    method. The structure is deliberately data-only so it survives
    pickling, can be inspected from tests, and stays cheap to compare
    across variations of the same module graph.

    Attributes:
        provide: The DI token consumers will inject. Accepts any
            hashable value — a class, a string, or a :class:`Token`.
        kind: One of ``"value"``, ``"class"``, ``"factory"``,
            ``"existing"``. Determines which payload field the
            container reads.
        value: Set when ``kind == "value"`` — the literal value to
            bind. Otherwise ``None``.
        use_class: Set when ``kind == "class"`` — the class to
            instantiate when the token is resolved.
        factory: Set when ``kind == "factory"`` — a callable that
            produces the value. May be sync or async; lauren awaits
            coroutines automatically.
        inject: Tuple of tokens (in argument order) handed to the
            factory at instantiation time. Bare tokens may be
            classes, strings, or :class:`Token` instances. To declare
            an *optional* dependency wrap the token in
            :class:`OptionalDep`.
        existing: Set when ``kind == "existing"`` — the token whose
            already-resolved instance becomes the bound value.
        scope: Resolution scope (singleton / request / transient).
            Defaults to singleton, matching NestJS.
        multi: When ``True``, the token participates in lauren's
            multi-binding collection so consumers requesting
            ``list[T]`` see this provider alongside others.
    """

    provide: Any
    kind: str
    value: Any = None
    use_class: type | None = None
    factory: Callable[..., Any] | None = None
    inject: tuple[Any, ...] = ()
    existing: Any = None
    scope: Scope = Scope.SINGLETON
    multi: bool = False
    name: str = ""  # human-readable label used in errors / logs

    def __post_init__(self) -> None:  # pragma: no cover - defensive guard
        valid = {"value", "class", "factory", "existing"}
        if self.kind not in valid:
            raise ValueError(
                f"CustomProvider.kind must be one of {sorted(valid)}, "
                f"got {self.kind!r}",
            )


@dataclass(frozen=True)
class OptionalDep:
    """Mark a member of an ``injects=[...]`` list as optional.

    When the matching provider is missing at resolution time, lauren
    passes ``None`` to the factory's positional argument instead of
    raising :class:`MissingProviderError`. NestJS exposes the same
    capability via ``{ token, optional: true }``; lauren keeps the
    affordance type-safe with a small wrapper class so the inject
    list reads top-to-bottom.

    Example::

        use_factory(
            provide="CONNECTION",
            factory=lambda opts, optional_logger=None: ...,
            injects=[Options, OptionalDep("LOGGER")],
        )
    """

    token: Any


# ---------------------------------------------------------------------------
# Public factory helpers
# ---------------------------------------------------------------------------


def _validate_token(token: Any, *, where: str) -> None:
    """Raise a clear error if ``token`` cannot be used as a DI key."""
    try:
        hash(token)
    except TypeError as exc:
        raise DecoratorUsageError(
            f"{where}: provider token {token!r} is not hashable. "
            "DI tokens must be classes, strings, or Token instances.",
            detail={"token": repr(token)},
        ) from exc
    if token is None:
        raise DecoratorUsageError(
            f"{where}: provider token must not be None.",
        )


def use_value(
    *,
    provide: Any,
    value: Any,
    multi: bool = False,
) -> CustomProvider:
    """Bind ``provide`` to a pre-built value.

    The value is treated as a singleton (no factory ever runs) and is
    returned by the container on every ``resolve()`` call. Common
    uses:

    * inject a mock service in tests::

          use_value(provide=CatsService, value=mock_cats_service)

    * register an externally-constructed object as a DI citizen::

          use_value(provide="REDIS", value=redis.from_url(...))

    * expose a literal config dict to handlers::

          use_value(provide="CONFIG", value={"debug": True})

    ``multi=True`` is supported for completeness — multiple
    :func:`use_value` registrations sharing a token can be assembled
    into a list with ``list[T]`` consumers, although the more common
    multi-binding pattern is on services rather than values.
    """
    _validate_token(provide, where="use_value")
    return CustomProvider(
        provide=provide,
        kind="value",
        value=value,
        multi=multi,
        name="useValue",
    )


def use_class(
    *,
    provide: Any,
    use: type,
    scope: Scope = Scope.SINGLETON,
    multi: bool = False,
) -> CustomProvider:
    """Bind ``provide`` to a class — typically *different* from the token.

    The classic example is environment-conditional configuration::

        configServiceProvider = use_class(
            provide=ConfigService,
            use=DevelopmentConfigService if dev else ProductionConfigService,
        )

    The chosen class is constructed through the standard DI pipeline,
    so its own ``__init__`` parameters resolve as if it had been
    registered via ``@injectable``. It does not need to itself carry
    the ``@injectable`` decoration: lauren auto-marks classes used in
    ``use_class`` with the matching scope so the factory machinery
    works end-to-end.
    """
    _validate_token(provide, where="use_class")
    if not isinstance(use, type):
        raise DecoratorUsageError(
            f"use_class.use must be a class; got {use!r}",
            detail={"use": repr(use)},
        )
    return CustomProvider(
        provide=provide,
        kind="class",
        use_class=use,
        scope=scope,
        multi=multi,
        name="useClass",
    )


def use_factory(
    *,
    provide: Any,
    factory: Callable[..., Any],
    injects: Iterable[Any] = (),
    scope: Scope = Scope.SINGLETON,
    multi: bool = False,
) -> CustomProvider:
    """Bind ``provide`` to the result of calling ``factory``.

    ``injects`` lists the tokens lauren resolves and passes *positionally*
    to the factory in declaration order. This positional contract is
    deliberately the only one supported — it keeps the call site small
    and unambiguous, and matches NestJS's `inject:` semantics::

        use_factory(
            provide="CONNECTION",
            factory=lambda opts, log: DatabaseConnection(opts.get(), log),
            injects=[OptionsProvider, "LOGGER"],
            #           ^^^               ^^^^^^^
            #           class token       string token
        )

    Wrap any entry in :class:`OptionalDep` to soften the resolution::

        injects=[OptionsProvider, OptionalDep("LOGGER")]

    Async factories work transparently — lauren awaits the return value
    when it's a coroutine, so an ``async def`` factory is just a
    factory.
    """
    _validate_token(provide, where="use_factory")
    if not callable(factory):
        raise DecoratorUsageError(
            f"use_factory.factory must be callable; got {factory!r}",
            detail={"factory": repr(factory)},
        )
    inject_tuple = tuple(injects)
    for tok in inject_tuple:
        if isinstance(tok, OptionalDep):
            _validate_token(tok.token, where="use_factory.inject")
        else:
            _validate_token(tok, where="use_factory.inject")
    return CustomProvider(
        provide=provide,
        kind="factory",
        factory=factory,
        inject=inject_tuple,
        scope=scope,
        multi=multi,
        name="useFactory",
    )


def use_existing(
    *,
    provide: Any,
    existing: Any,
) -> CustomProvider:
    """Alias ``provide`` to an already-registered ``existing`` token.

    Both tokens resolve to the same instance under singleton scope.
    Multiple aliases can chain through several ``use_existing`` rows
    — lauren walks the chain at resolve time and rejects cycles loudly.
    Aliases inherit the existing provider's scope.

    Typical use case: expose the same logger under two names so
    legacy code injecting ``"AliasedLoggerService"`` keeps working
    while new code injects the class directly::

        use_existing(provide="AliasedLoggerService", existing=LoggerService)
    """
    _validate_token(provide, where="use_existing")
    _validate_token(existing, where="use_existing")
    if provide == existing:
        raise DecoratorUsageError(
            f"use_existing cannot alias a token to itself; both sides are {existing!r}",
        )
    return CustomProvider(
        provide=provide,
        kind="existing",
        existing=existing,
        name="useExisting",
    )


# ---------------------------------------------------------------------------
# Helpers used by the registration pipeline
# ---------------------------------------------------------------------------


def normalise_provider_token(p: Any) -> Any:
    """Return the DI token a ``providers=[...]`` entry exposes.

    * a class           → the class itself
    * a function        → the function (function-provider token)
    * a CustomProvider  → its :attr:`CustomProvider.provide` field
    * any other hashable → returned untouched

    Used during module-graph construction so the export validator and
    the visibility set work uniformly across standard and custom
    providers. Callers should treat unrecognised values as a
    programming error and surface a typed exception.
    """
    if isinstance(p, CustomProvider):
        return p.provide
    return p


def is_custom_provider(p: Any) -> bool:
    """Convenience predicate used by the module-graph compiler."""
    return isinstance(p, CustomProvider)


__all__ = [
    "Token",
    "Inject",
    "_InjectMarker",
    "OptionalDep",
    "CustomProvider",
    "use_value",
    "use_class",
    "use_factory",
    "use_existing",
    "normalise_provider_token",
    "is_custom_provider",
]
