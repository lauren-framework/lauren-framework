# Events

Typed application events: `@on_event` handlers, the per-app `EventBus` and the request-scoped `EventPublisher`.

## Handlers

### `on_event`

Subscribe a provider method to an event class; it is discovered at startup.

``event_type=None`` infers the class from the annotation of the first
parameter after ``self`` (a union subscribes to each member). The
handler also receives instances of subclasses. Other parameters are an
:class:`~lauren.events.EventContext` (any name) or dependencies the
provider's module can resolve.

``mode`` is ``"inline"`` (awaited inside ``emit()``, part of the
publisher's unit of work), ``"concurrent"`` (a tracked task started
after the inline handlers succeed) or ``"background"`` (after the HTTP
response when published through an ``EventPublisher`` in a request,
otherwise like ``"concurrent"``). Higher ``priority`` runs first; ties
keep discovery order. ``errors`` defaults to ``"propagate"`` for inline
handlers (the exception aborts the emission and re-raises from
``emit()``) and ``"isolate"`` for deferred ones (logged, signalled as
:class:`~lauren.signals.EventHandlerFailed`, returned in
:attr:`EmitResult.errors <lauren.events.EmitResult.errors>`).
``enabled=False`` keeps the handler validated but never calls it.

The decorator only records metadata and returns the method unchanged;
stacking it subscribes the method to several events.

### `EventContext`

Delivery details for one handler call, injected into a parameter annotated ``EventContext``.

A handler declares it under any name, after the event parameter::

    @on_event()
    async def record(self, event: OrderPlaced, ctx: EventContext) -> None:
        ctx.log.log(f"order {event.order_id} ({ctx.event_id})")

The request is deliberately not exposed: requests are pooled and
reused, and deferred handlers outlive them. Put whatever a handler
needs from the request into the event.

## Publishing

### `EventBus`

The application's event bus: one per app, injectable everywhere as a singleton.

``emit()`` delivers an event to every ``@on_event`` handler subscribed
to its class or a base class. Inline handlers run first, by priority,
inside the call — sharing one ``OperationScope`` per emission for their
request-scoped dependencies; then deferred (``"concurrent"`` /
``"background"``) handlers start as tracked tasks, each in its own
``OperationScope``. Use :class:`EventPublisher` instead to run inline
handlers in the current request's scope.

``recording()``, ``drain()`` and ``disabled()`` are testing aids. A bus
built directly (``EventBus()``) has no handlers, which makes it a
convenient stand-in when unit-testing a service that publishes.

### `EventPublisher`

The request-scoped publisher: inline handlers join the current request's DI scope.

Inject it (``events: EventPublisher``) into a route handler, a
WebSocket hook, a request-scoped provider or an ``@on_event`` handler —
it is provided by the framework. ``emit()`` behaves like
:meth:`EventBus.emit`, except that inline handlers resolve
``Scope.REQUEST`` dependencies from the publisher's scope (the same
``DbSession`` as the controller), and ``"background"`` handlers in an
HTTP request run after the response is sent, on the request's
``BackgroundTasks`` queue, each in a fresh ``OperationScope``. In a
WebSocket connection there is no response, so background handlers run
like concurrent ones.

Resolving it anywhere else (a singleton, a job, ``app.scope()``) raises
:class:`~lauren.exceptions.LifecycleViolationError` — inject
:class:`EventBus` there.

### `EmitResult`

What one ``emit()`` call did.

``handled`` counts handlers that finished without an error during the
call (inline handlers, plus deferred ones when ``wait=True``);
``scheduled`` counts deferred handlers queued or started; ``errors``
holds an :class:`~lauren.signals.EventHandlerFailed` record for every
``"isolate"`` handler that failed during the call.

## After-commit delivery

### `TransactionPhase`

When a deferred emission (``EventPublisher.emit(after=...)``) is delivered.

### `UnitOfWork`

A transaction boundary that ``EventPublisher.emit(after=TransactionPhase.COMMIT)`` can wait for.

Core defines only the contract; a database integration (for example a
request-scoped session) implements it and is registered as a provider
— either with ``provides=(UnitOfWork,)`` or simply by having both
methods, since the protocol also matches structurally. The unit of
work awaits each callback once: ``on_commit`` callbacks after a
successful commit, ``on_rollback`` callbacks after a rollback.

## Testing

### `EventRecording`

Events and handler outcomes captured by :meth:`EventBus.recording() <lauren.events.EventBus.recording>`.

Everything is recorded in order: ``events`` as they are emitted,
``calls`` and ``failures`` as handlers finish.

## Configuration

### `EventsModule`

Optional tuning for the application event bus; events work without it.

Import the module ``for_root`` returns into any module of the app.
