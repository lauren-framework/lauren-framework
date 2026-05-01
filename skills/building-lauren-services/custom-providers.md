# Lauren Custom Providers — Reference

Custom providers go beyond `@injectable` classes. They let you bind string/token keys and inject externally-constructed values.

## Token — opaque DI key

```python
from lauren._di.custom import Token

DB_URL = Token("DB_URL")       # unique by identity (default)
SHARED = Token("X", unique=False)  # equality-based, shareable across modules
```

Tokens are hashable and safe as dict keys. Two `Token("X")` instances are **different** by default.

## Inject — annotated token resolution

Consume a string or `Token` key via `Inject`:

```python
from typing import Annotated
from lauren._di.custom import Inject

class MyService:
    def __init__(self, url: Annotated[str, Inject(DB_URL)]) -> None:
        self._url = url
```

## use_value — pre-built value

```python
from lauren import use_value

@module(providers=[
    use_value(provide="CONFIG", value={"debug": True}),
    use_value(provide=DB_URL, value="postgresql://localhost/mydb"),
])
class InfraModule:
    pass
```

## use_class — bind token to a different class

```python
from lauren import use_class

config_provider = use_class(
    provide=ConfigService,
    use=DevConfigService if dev else ProdConfigService,
)

@module(providers=[config_provider], exports=[ConfigService])
class AppModule:
    pass
```

The chosen class is constructed through DI — its `__init__` params are resolved automatically.

## use_factory — compute value from a function

```python
from lauren import use_factory

@module(providers=[
    use_factory(
        provide="CONNECTION",
        factory=lambda opts, logger: DatabaseConnection(opts.url, logger),
        inject=[OptionsService, "LOGGER"],
        scope=Scope.SINGLETON,
    )
])
class DatabaseModule:
    pass
```

- `inject` items are resolved positionally and passed to `factory` as positional args.
- Async factories are awaited automatically.
- Wrap with `OptionalDep(token)` to make an inject entry optional.

## use_existing — alias one token to another

```python
from lauren import use_existing

@module(providers=[
    use_existing(provide="CACHE_ALIAS", existing=CacheService),
])
class CacheModule:
    pass
```

Both tokens resolve to the same instance. Chains are walked at resolve time; cycles raise an error.

## Complete example — string token pattern

```python
from typing import Annotated
from lauren import module, injectable, use_value
from lauren._di.custom import Inject, Token

SECRET_KEY = Token("SECRET_KEY")

@injectable()
class AuthService:
    def __init__(self, key: Annotated[str, Inject(SECRET_KEY)]) -> None:
        self._key = key

@module(
    providers=[
        use_value(provide=SECRET_KEY, value=os.environ["SECRET_KEY"]),
        AuthService,
    ],
    exports=[AuthService],
)
class AuthModule:
    pass
```
