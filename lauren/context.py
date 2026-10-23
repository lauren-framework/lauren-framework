"""The application without HTTP: standalone contexts and DI scopes for non-request work.

An :class:`ApplicationContext` boots the same validated graph as the server —
``@post_construct`` on enter, ``on_shutdown`` and ``@pre_destruct`` on exit —
for scripts, one-off tasks, commands and workers::

    async with LaurenFactory.create_context(AppModule) as ctx:
        users = ctx.get(UserService)                    # a constructed singleton
        async with ctx.scope("backfill") as scope:
            await users.backfill(await scope.resolve(DbSession))

An :class:`OperationScope` is the request scope without a request. Open one
with :meth:`LaurenApp.scope() <lauren.LaurenApp.scope>` (or ``ctx.scope()``)
around a script, a job run or any other unit of work that resolves
``Scope.REQUEST`` providers::

    async with app.scope("backfill") as scope:
        db = await scope.resolve(DbSession)          # one instance per scope
        assert db is await scope.resolve(DbSession)
    # exit: @pre_destruct, then aclose(), newest instance first

Scopes are cheap (one dict plus teardown, the same as an HTTP request), do not
nest or inherit from each other, and are owned by the app's container, so two
apps in one process never share instances.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import signal
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Any, TextIO

from ._commands._context import CommandResult
from ._di import _GeneratorContextWrapper, _describe, _multi_binding_element_type
from ._di._scope import finalize_scope
from .exceptions import DestructError, DIScopeViolationError, LaurenError, LifecycleViolationError
from .types import Scope

if TYPE_CHECKING:
    from ._app import Lauren
    from ._asgi import LaurenApp
    from ._commands._compile import CommandInfo
    from ._di import DIContainer, Provider

logger = logging.getLogger("lauren")

_PENDING = "pending"
_ACTIVE = "active"
_CLOSED = "closed"


class OperationScope:
    """A named DI scope whose ``Scope.REQUEST`` providers live as long as the scope.

    Obtain one from :meth:`LaurenApp.scope() <lauren.LaurenApp.scope>` and use it
    as an async context manager; it is single-use.

    * ``Scope.REQUEST`` instances are cached per scope; singletons come from the
      container; transients are new on every resolution and are not tracked.
    * ``values`` seeds the scope's framework values: a token found there is
      returned as-is, both from :meth:`resolve` and for a provider's constructor
      parameter of that type.
    * On exit, every request-scoped instance is torn down in reverse creation
      order — its ``@pre_destruct`` hook, then ``aclose()`` (generator
      providers' teardown) — with the same finaliser HTTP requests use.
      Teardown errors are logged and collected in :attr:`teardown_errors`;
      they are never raised, and an exception from the ``async with`` body
      propagates unchanged.

    Several coroutines of one operation may share a scope. As within one HTTP
    request, two of them resolving the same request-scoped token for the
    first time concurrently may each construct an instance.
    """

    __slots__ = ("_name", "_container", "_cache", "_values", "_state", "_teardown_errors")

    def __init__(
        self,
        container: DIContainer,
        name: str = "operation",
        *,
        values: Mapping[Any, Any] | None = None,
    ) -> None:
        self._name = name
        self._container = container
        self._cache: dict[Any, Any] = {}
        self._values: dict[Any, Any] = dict(values or {})
        self._state = _PENDING
        self._teardown_errors: tuple[DestructError, ...] = ()

    @property
    def name(self) -> str:
        """The label given to :meth:`LaurenApp.scope`, used in logs and errors."""
        return self._name

    @property
    def values(self) -> Mapping[Any, Any]:
        """A read-only view of the framework values seeded with ``values=``."""
        return MappingProxyType(self._values)

    @property
    def active(self) -> bool:
        """``True`` between entering and leaving the ``async with`` block."""
        return self._state == _ACTIVE

    @property
    def teardown_errors(self) -> tuple[DestructError, ...]:
        """Teardown failures collected when the scope closed (empty until then).

        Each entry is a :class:`~lauren.exceptions.DestructError` whose
        ``__cause__`` is the exception the hook raised; ``detail`` carries
        ``class``, ``stage`` (``"pre_destruct"`` or ``"aclose"``), ``origin``
        and ``cause``.
        """
        return self._teardown_errors

    async def resolve(self, token: Any, *, owning_module: type | None = None) -> Any:
        """Resolve ``token`` within this scope.

        Visibility is global by default (``owning_module=None``); pass a module
        class to resolve only what that module can see, as its own providers
        would. Raises :class:`~lauren.exceptions.LifecycleViolationError`
        outside the ``async with`` block.
        """
        if self._state != _ACTIVE:
            raise LifecycleViolationError(
                f"OperationScope {self._name!r} is {self._state}; resolve() works only inside "
                f"`async with app.scope(...) as scope:`",
                detail={"scope": self._name, "state": self._state},
            )
        return await self._container.resolve(
            token,
            request_cache=self._cache,
            framework_values=self._values,
            owning_module=owning_module,
        )

    async def __aenter__(self) -> OperationScope:
        if self._state != _PENDING:
            raise LifecycleViolationError(
                f"OperationScope {self._name!r} was already entered; scopes are single-use, "
                "open a new one with app.scope()",
                detail={"scope": self._name, "state": self._state},
            )
        self._state = _ACTIVE
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._state = _CLOSED
        cache, self._cache = self._cache, {}
        errors = await finalize_scope(self._container, cache, origin=f"scope {self._name!r}")
        self._teardown_errors = tuple(errors)

    def __repr__(self) -> str:
        return f"<OperationScope {self._name!r} {self._state}, {len(self._cache)} request-scoped instance(s)>"


_STARTING = "starting"


class ApplicationContext:
    """A built app run without HTTP: ``async with`` starts it and shuts it down.

    Create one with :meth:`LaurenFactory.create_context`, :meth:`LaurenApp.context`
    or :meth:`from_lauren`. Entering runs :meth:`LaurenApp.startup` —
    ``@post_construct`` hooks, ``Lauren.on_startup`` hooks, and hosted services
    and the scheduler only when the context enables them (both default off,
    and the app must have them enabled too). Leaving runs
    :meth:`LaurenApp.shutdown` (``on_shutdown`` callbacks, ``@pre_destruct``);
    an exception from the block propagates after shutdown. A startup failure
    shuts down whatever had started, then propagates. Contexts are
    single-use, and an app has exactly one lifecycle owner: wrapping an app
    that was already started raises :class:`~lauren.exceptions.LifecycleViolationError`.
    """

    __slots__ = ("_app", "_hosted_services", "_scheduler", "_drain_timeout", "_state")

    def __init__(
        self,
        app: LaurenApp,
        *,
        hosted_services: bool = False,
        scheduler: bool = False,
        drain_timeout: float = 10.0,
    ) -> None:
        _require_unstarted(app)
        self._app = app
        self._hosted_services = bool(hosted_services)
        self._scheduler = bool(scheduler)
        self._drain_timeout = drain_timeout
        self._state = _PENDING

    @classmethod
    def from_lauren(
        cls,
        lauren_app: Lauren,
        *,
        hosted_services: bool = False,
        scheduler: bool = False,
        drain_timeout: float = 10.0,
    ) -> ApplicationContext:
        """Compile an imperative :class:`~lauren.Lauren` app (without starting it) and wrap it."""
        return cls(
            lauren_app._compile_now(),
            hosted_services=hosted_services,
            scheduler=scheduler,
            drain_timeout=drain_timeout,
        )

    # -- Introspection -----------------------------------------------------

    @property
    def app(self) -> LaurenApp:
        """The wrapped :class:`~lauren.LaurenApp` (an escape hatch)."""
        return self._app

    @property
    def container(self) -> DIContainer:
        """The app's :class:`~lauren.DIContainer` (an escape hatch)."""
        return self._app.container

    @property
    def active(self) -> bool:
        """``True`` between a successful enter and the exit."""
        return self._state == _ACTIVE

    # -- Lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> ApplicationContext:
        if self._state != _PENDING:
            raise LifecycleViolationError(
                f"this ApplicationContext is {self._state}; contexts are single-use",
                detail={"state": self._state, "reason": "context_reused"},
            )
        app = self._app
        _require_unstarted(app)
        if app._scheduling is not None:
            app._scheduling.restrict(hosted_services=self._hosted_services, scheduler=self._scheduler)
        self._state = _STARTING
        try:
            await app.startup()
        except BaseException:
            self._state = _CLOSED
            try:
                await app.shutdown(drain_timeout=self._drain_timeout)
            except Exception:
                logger.exception("ApplicationContext: shutdown after a failed startup raised")
            raise
        self._state = _ACTIVE
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._state == _CLOSED:
            return
        self._state = _CLOSED
        await self._app.shutdown(drain_timeout=self._drain_timeout)

    def _require_active(self, what: str) -> None:
        if self._state != _ACTIVE:
            raise LifecycleViolationError(
                f"ApplicationContext.{what}() works only inside `async with ctx:` (the context is {self._state})",
                detail={"method": what, "state": self._state, "reason": "context_not_active"},
            )

    # -- Resolution --------------------------------------------------------

    def get(self, token: Any) -> Any:
        """Return the already constructed singleton bound to ``token`` (synchronous).

        Raises :class:`~lauren.exceptions.MissingProviderError` for an unknown
        token and :class:`~lauren.exceptions.DIScopeViolationError`
        (``reason="not_singleton"``) for a request-scoped or transient one —
        use :meth:`resolve` or :meth:`scope` for those. A ``list[T]`` token or a
        multi-bound token returns every bound singleton.
        """
        self._require_active("get")
        container = self._app.container
        element = _multi_binding_element_type(token)
        if element is not None:
            bindings = list(container._token_bindings.get(element) or ())  # noqa: SLF001
            if not bindings:
                container.get_provider(element)  # raises MissingProviderError
            return [_constructed(container, provider) for provider in bindings]
        provider = container.get_provider(token)
        bindings = container._token_bindings.get(token) or []  # noqa: SLF001
        if len(bindings) > 1:  # get_provider() already rejected a non-multi ambiguity
            return [_constructed(container, b) for b in bindings]
        return _constructed(container, provider)

    async def resolve(self, token: Any, *, owning_module: type | None = None) -> Any:
        """Resolve ``token`` (a singleton, or a new transient instance).

        ``Scope.REQUEST`` tokens need a scope: they raise
        :class:`~lauren.exceptions.DIScopeViolationError`
        (``reason="no_active_scope"``) — resolve them through :meth:`scope`.
        """
        self._require_active("resolve")
        container = self._app.container
        provider = _provider_or_none(container, token, owning_module)
        if provider is not None and provider.scope == Scope.REQUEST:
            name = _describe(provider.cls)
            raise DIScopeViolationError(
                f"{name} is request-scoped; resolve it inside a scope: "
                f"`async with ctx.scope() as scope: await scope.resolve({name})`",
                detail={"token": name, "reason": "no_active_scope"},
            )
        return await container.resolve(token, owning_module=owning_module)

    def scope(self, name: str = "context", *, values: Mapping[Any, Any] | None = None) -> OperationScope:
        """Open an :class:`OperationScope` (same as :meth:`LaurenApp.scope`)."""
        self._require_active("scope")
        return self._app.scope(name, values=values)

    # -- Commands ----------------------------------------------------------

    def commands(self, *, include_hidden: bool = False) -> tuple[CommandInfo, ...]:
        """The app's ``@command`` methods, sorted by name (no need to enter the context)."""
        return self._app._commands.infos(include_hidden=include_hidden)

    async def run_command(
        self,
        name: str,
        *argv: object,
        stdin: TextIO | None = None,
        interactive: bool = False,
        assume_yes: bool = False,
        verbose: bool = False,
    ) -> CommandResult:
        """Run command ``name`` with CLI arguments ``argv`` and capture its outcome.

        Behaves like ``python -m lauren exec``: the command gets a fresh
        :class:`OperationScope`, ``--yes`` / ``--verbose`` in ``argv`` are runner
        flags (unless the command declares them), and failures become exit
        codes — 2 for an unknown command or bad arguments, 1 for an uncaught
        exception — instead of exceptions. Output written through
        ``CommandContext.echo`` / ``error`` is captured in the result.
        ``interactive=True`` with ``stdin=io.StringIO("y\\n")`` answers prompts.
        """
        self._require_active("run_command")
        out, err = io.StringIO(), io.StringIO()
        outcome = await self._app._commands.run(
            self._app,
            name,
            [str(arg) for arg in argv],
            stdout=out,
            stderr=err,
            stdin=stdin,
            interactive=interactive,
            assume_yes=assume_yes,
            verbose=verbose,
        )
        return CommandResult(
            exit_code=outcome.exit_code,
            value=outcome.value,
            stdout=out.getvalue(),
            stderr=err.getvalue(),
            exception=outcome.exception,
        )

    # -- Workers -----------------------------------------------------------

    async def wait_for_shutdown_signal(self, signals: Iterable[int] = (signal.SIGINT, signal.SIGTERM)) -> int:
        """Block until SIGINT or SIGTERM arrives, then return the signal number.

        The handlers are installed for this call only and removed before it
        returns, so leaving ``async with`` right after performs the graceful
        shutdown, and a second signal gets the default behaviour (the process
        exits). Typical worker::

            async with LaurenFactory.create_context(WorkerModule, hosted_services=True) as ctx:
                await ctx.wait_for_shutdown_signal()
        """
        self._require_active("wait_for_shutdown_signal")
        loop = asyncio.get_running_loop()
        received: asyncio.Future[int] = loop.create_future()

        def _on_signal(signum: int) -> None:
            if not received.done():
                received.set_result(signum)

        restore, installed = _install_signal_handlers(loop, signals, _on_signal)
        if not installed:
            logger.warning(
                "ApplicationContext.wait_for_shutdown_signal(): no signal handler could be installed "
                "(not the main thread?); it returns only when cancelled"
            )
        try:
            signum = await received
        finally:
            restore()
        self._app.logger.log(
            f"Signal {_signal_name(signum)} received; shutting down",
            context="ApplicationContext",
            signal=_signal_name(signum),
        )
        return signum

    def __repr__(self) -> str:
        return f"<ApplicationContext {self._state}>"


def _require_unstarted(app: LaurenApp) -> None:
    if app._started or app._shutdown_running:
        state = "shut down" if app._shutdown_running else "already started"
        raise LifecycleViolationError(
            f"cannot wrap an app that is {state} in an ApplicationContext: the context owns startup and "
            "shutdown. Build a fresh app (LaurenFactory.create_context(...)) or skip app.startup()",
            detail={"reason": "app_already_started" if app._started else "app_shut_down"},
        )


def _constructed(container: DIContainer, provider: Provider) -> Any:
    """The constructed singleton of ``provider`` (aliases followed)."""
    seen: set[int] = set()
    while provider.provider_kind == "existing" and id(provider) not in seen:
        seen.add(id(provider))
        provider = container.get_provider(provider.alias_target)
    name = _describe(provider.cls)
    if provider.scope != Scope.SINGLETON:
        raise DIScopeViolationError(
            f"{name} is {provider.scope.label}-scoped; ctx.get() returns constructed singletons only. "
            "Use `await ctx.resolve(...)` for transients and `ctx.scope()` for request-scoped providers",
            detail={"token": name, "scope": provider.scope.label, "reason": "not_singleton"},
        )
    key: Any = id(provider) if provider.multi else provider.cls
    singletons = container._singletons  # noqa: SLF001 - read-only peek, no construction
    if key not in singletons:
        raise LifecycleViolationError(
            f"{name} has not been constructed (is the context started?)",
            detail={"token": name, "reason": "not_constructed"},
        )
    value = singletons[key]
    return value.value if isinstance(value, _GeneratorContextWrapper) else value


def _provider_or_none(container: DIContainer, token: Any, owning_module: type | None) -> Provider | None:
    """The single provider ``token`` resolves to, or ``None`` (lists, ambiguity, unknown)."""
    try:
        provider = container.get_provider(token, owning_module=owning_module)
        seen: set[int] = set()
        while provider.provider_kind == "existing" and id(provider) not in seen:
            seen.add(id(provider))
            provider = container.get_provider(provider.alias_target)
    except LaurenError:
        return None  # container.resolve() raises the precise error
    return provider


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:  # pragma: no cover - only real signal numbers reach here
        return str(signum)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    signals: Iterable[int],
    callback: Callable[[int], None],
) -> tuple[Callable[[], None], bool]:
    """Install ``callback(signum)`` for ``signals`` on ``loop``; return ``(restore, installed_any)``.

    Uses ``loop.add_signal_handler`` where the loop supports it, else
    ``signal.signal`` (Windows); outside the main thread nothing can be
    installed. ``restore()`` puts the previous handlers back.
    """
    undo: list[Callable[[], None]] = []

    def _from_signal_module(signum: int, frame: Any) -> None:
        loop.call_soon_threadsafe(callback, signum)

    for sig in signals:
        previous = signal.getsignal(sig)
        try:
            loop.add_signal_handler(sig, callback, sig)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(sig, _from_signal_module)
            except (ValueError, OSError):
                continue
            undo.append(functools.partial(_restore_handler, sig, previous))
        else:

            def _undo(sig: int = sig, previous: Any = previous) -> None:
                loop.remove_signal_handler(sig)
                _restore_handler(sig, previous)

            undo.append(_undo)

    def restore() -> None:
        for step in reversed(undo):
            try:
                step()
            except (ValueError, OSError, RuntimeError):  # pragma: no cover - loop already closed
                pass

    return restore, bool(undo)


def _restore_handler(sig: int, previous: Any) -> None:
    if previous is not None:
        signal.signal(sig, previous)


__all__ = ["ApplicationContext", "OperationScope"]
