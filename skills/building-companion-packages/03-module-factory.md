# 03 — Module Factory Pattern

Every companion package exposes its wiring through a **module factory** —
a classmethod on a class (conventionally named `<Domain>Module`) that accepts
configuration, wires the DI providers, and returns a fully-configured `@module`
instance ready to be placed in `imports=[]`.

The pattern is borrowed from NestJS's `forRoot()` / `forRootAsync()` and is
used throughout the Lauren ecosystem: `LLMModule.for_root(cfg)`,
`StaticFilesModule.for_root(path, directory=…)`, etc.

## Minimal factory

```python
from lauren import module, use_value

class GreeterModule:
    @classmethod
    def for_root(cls, greeting: str = "Hello") -> type:

        @module(
            providers=[use_value(provide="GREETING", value=greeting)],
            exports=["GREETING"],
        )
        class _GreeterModule:
            pass

        return _GreeterModule
```

Host usage:

```python
@module(imports=[GreeterModule.for_root("Hi")])
class AppModule:
    pass
```

## Full factory with `use_value`, `use_class`, `use_factory`

Real companion modules typically combine all three provider kinds:

```python
from lauren import module, Scope, use_value, use_class, use_factory
from ._config import LLMConfig
from ._transport import Transport, AnthropicTransport, MockTransport
from ._runner import AgentRunner

class LLMModule:
    """Configure the LLM transport and agent runner.

    Usage::

        cfg = LLMConfig(provider="anthropic", model="claude-opus-4-7", api_key="sk-…")
        LLMModule.for_root(cfg)                  # production
        LLMModule.for_root(cfg, transport_override=mock)  # tests
    """

    @classmethod
    def for_root(
        cls,
        config: LLMConfig,
        *,
        transport_override: Transport | None = None,
    ) -> type:
        def _make_transport(cfg: LLMConfig) -> Transport:
            if transport_override is not None:
                return transport_override
            if cfg.provider == "anthropic":
                return AnthropicTransport(cfg)
            raise ValueError(f"Unknown provider: {cfg.provider}")

        @module(
            providers=[
                # Literal config value — no constructor called
                use_value(provide=LLMConfig, value=config),

                # Build transport via factory so the provider choice
                # happens at startup, not at class-definition time
                use_factory(
                    provide=Transport,
                    factory=_make_transport,
                    injects=[LLMConfig],
                    scope=Scope.SINGLETON,
                ),

                # AgentRunner is a plain @injectable — use_class lets us
                # swap it with a mock in tests without changing the token
                use_class(
                    provide=AgentRunner,
                    use=AgentRunner,
                    scope=Scope.SINGLETON,
                ),
            ],
            exports=[LLMConfig, Transport, AgentRunner],
        )
        class _LLMModule:
            pass

        _LLMModule.__name__ = "LLMModule"
        return _LLMModule

    @classmethod
    def for_testing(cls) -> type:
        """Return a module with a MockTransport pre-wired.

        The mock transport queues canned responses and records every
        CompletionRequest so tests can assert on what was sent::

            cfg, mock = LLMConfig.for_testing()
            mock.queue_response(_make_completion("hello"))
            client = TestClient(LaurenFactory.create(AppModule))
        """
        cfg, mock = LLMConfig.for_testing()
        return cls.for_root(cfg, transport_override=mock)
```

## Naming the inner module

Give the dynamically-created inner class a human-readable `__name__` so it
shows up clearly in startup logs and error messages:

```python
_LLMModule.__name__ = "LLMModule"       # appears in log lines
_LLMModule.__qualname__ = "LLMModule"   # appears in tracebacks
```

## `for_testing()` convention

Every companion module should expose a `for_testing()` classmethod that wires
safe, zero-external-calls defaults.  This lets host applications write
integration tests without setting up real infrastructure:

```python
# Pattern used in lauren-ai
cfg, mock = LLMConfig.for_testing()
# cfg.provider == "mock", mock is a MockTransport instance
# mock.queue_response(…) / mock.queue_tool_use(…)
```

Host app tests:

```python
@module(imports=[LLMModule.for_testing()])
class TestModule:
    pass

client = TestClient(LaurenFactory.create(TestModule))
```

## Export rules

Only export tokens the host application should be able to inject.  Internal
implementation details stay encapsulated:

```python
@module(
    providers=[
        use_value(provide=LLMConfig, value=config),  # internal
        use_factory(provide=Transport, …),            # exported
        AgentRunner,                                  # exported
        _InternalRetryPolicy,                         # NOT exported
    ],
    exports=[Transport, AgentRunner],  # only these are visible outside
)
class _LLMModule:
    pass
```
