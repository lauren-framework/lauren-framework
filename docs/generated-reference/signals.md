# Signals

POSIX signal integration and application shutdown hooks.

### `SignalBus`

```python
class SignalBus(logger: Logger | None = None)
```

In-process pub/sub bus for :class:`LifecycleEvent` subclasses.

The bus is owned by a :class:`LaurenApp` — there is one bus per
application so multiple apps in the same process do not share
listeners. The bus is thread-agnostic in that listener lookup
is a plain dict read; concurrent emission from multiple
coroutines is safe because each :meth:`emit` iterates its own
listener list snapshot.

Listener discipline
-------------------

* Listener errors are *logged but swallowed*. Observability code
  must not be able to break the request path.
* Async listeners are awaited sequentially, not concurrently,
  so subsequent listeners see the side-effects of earlier ones
  in a deterministic order.
* A listener registered for a base-class event receives every
  instance of that class, including subclasses — so
  ``bus.on(LifecycleEvent)`` is a firehose of every event.

Basic usage::

    bus = app.signals  # SignalBus owned by the app

    @bus.on(RequestComplete)
    def log_slow(event: RequestComplete) -> None:
        if event.duration_s > 1.0:
            app.logger.warn("slow request", path=event.request.path)

#### `SignalBus.on`

```python
def on(self, event_type: type[E]) -> Callable[[Listener], Listener]
```

Register ``fn`` as a listener for ``event_type``.

Returns the original callable so ``@bus.on(StartupComplete)``
can decorate a function idiomatically. The callable may be
sync or async; both work identically from the caller's
perspective.

A listener is registered at most once per ``(event_type, fn)``
pair — re-registration is silently idempotent so module
reloads during development do not create duplicate
subscriptions.

#### `SignalBus.off`

```python
def off(self, event_type: type[E], fn: Listener) -> bool
```

Remove a previously-registered listener.

Returns ``True`` when the listener was removed, ``False``
when it wasn't registered in the first place. Never raises —
callers typically use this during test teardown where a
missing listener is not a bug.

#### `SignalBus.listener_count`

```python
def listener_count(self, event_type: type[LifecycleEvent]) -> int
```

Total listeners that would be invoked for ``event_type``.

Walks the MRO the same way :meth:`emit` does, so a listener
on ``LifecycleEvent`` is counted once for every concrete
subclass. Primarily useful for tests and diagnostics.

#### `SignalBus.clear`

```python
def clear(self) -> None
```

Remove every listener.

Primarily useful between tests when the same ``SignalBus``
is reused across cases.

#### `SignalBus.emit`

```python
def emit(self, event: LifecycleEvent) -> None
```

Fire ``event`` to every registered listener.

Listeners registered on any class in the event's MRO are
invoked, in registration order within a class and
most-specific-first across classes. Async listeners are
awaited sequentially; sync listeners run inline.

Exceptions inside listeners are logged through the bus's
:class:`~lauren.logging.Logger` and then swallowed so a
misbehaving listener can never break the request path.

#### `SignalBus.emit_sync`

```python
def emit_sync(self, event: LifecycleEvent) -> None
```

Synchronous variant of :meth:`emit`.

Used from framework code paths that are not themselves async
(startup / shutdown construction). Async listeners are
scheduled on the running loop when one is available and
otherwise skipped with a warning — we never create an event
loop just to run a listener, because doing so would subtly
break applications that rely on a specific loop policy.

## Lifecycle events

### `LifecycleEvent`

```python
class LifecycleEvent(timestamp: float = time.monotonic())
```

Base class for every event published on the :class:`SignalBus`.

Subclasses carry event-specific context (the live ``Request``,
the duration, the captured exception, ...). The base class only
holds a monotonic timestamp so listeners can compute deltas
without having to record wall-clock time themselves.

### `StartupBegin`

```python
class StartupBegin(timestamp: float = time.monotonic(), app: Any = None)
```

Emitted when the app receives its first ``lifespan.startup``.

At this point the DI graph is compiled but ``@post_construct``
hooks have NOT run yet. Listeners observing this event are
typically interested in recording "app build completed" telemetry
without depending on user services being constructed.

### `StartupComplete`

```python
class StartupComplete(timestamp: float = time.monotonic(), app: Any = None, duration_s: float = 0.0)
```

Fired once every ``@post_construct`` hook has finished running.

The app is fully ready to accept traffic at this point. Metrics
pipelines typically record a "ready" counter here; health-check
endpoints can be wired to flip a readiness flag.

### `RequestReceived`

```python
class RequestReceived(timestamp: float = time.monotonic(), request: Any = None)
```

Fired as soon as the ASGI scope has been parsed into a :class:`Request`.

The router has NOT yet run, so ``request.get_matched_route()``
returns ``None``. Listeners should avoid reading the body here
unless they also intend to be the sole consumer — doing so
would flip the body into buffered mode and defeat any downstream
:class:`~lauren.extractors.ByteStream` handler.

### `RequestComplete`

```python
class RequestComplete(timestamp: float = time.monotonic(), request: Any = None, response: Any = None, status: int = 0, duration_s: float = 0.0, error: BaseException | None = None)
```

Fired after the response has been fully sent to the client.

Fires on both success and error paths. When the handler raised
an exception that escaped the middleware chain, the exception
instance is attached to :attr:`error` and :attr:`status`
reflects the final HTTP status surfaced to the client.

### `ShutdownBegin`

```python
class ShutdownBegin(timestamp: float = time.monotonic(), app: Any = None)
```

Fired when ``lifespan.shutdown`` arrives or a signal triggered.

Listeners are typically used to flush buffers, close external
connections, or emit a final heartbeat. The event does NOT
block shutdown — listeners run concurrently with the framework's
own ``@pre_destruct`` machinery.
