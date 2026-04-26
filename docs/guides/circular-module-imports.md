# Circular Module Imports

> When two modules need to see each other's providers, a direct Python import causes a circular import error at startup.  Lauren's `ForwardRef` support lets you break the cycle by deferring module-class lookup to `LaurenFactory.create()` time — after every file is fully loaded.

## The problem

Consider a typical feature-module split:

```
users/
    module.py   ← UserModule
orders/
    module.py   ← OrderModule
```

`UserModule` exports a `UserRepo` that `OrderModule` needs.  Normally, `OrderModule` re-exports `UserRepo` so that `UserModule` can reach `OrderService` — creating a cycle:

```python
# orders/module.py
from users.module import UserModule        # ← works

@module(imports=[UserModule], exports=[UserRepo, OrderService])
class OrderModule: ...
```

```python
# users/module.py
from orders.module import OrderModule     # ← ImportError: circular import!

@module(imports=[OrderModule], providers=[UserRepo])
class UserModule: ...
```

Python resolves imports eagerly: when `users/module.py` is first imported it starts executing, sees `from orders.module import OrderModule`, imports `orders/module.py` which immediately tries to `from users.module import UserModule` — but `users/module.py` hasn't finished yet.  `UserModule` doesn't exist, and Python raises `ImportError`.

## The solution — `ForwardRef`

`ForwardRef("ClassName")` is Python's standard way to name a type that isn't yet bound.  Lauren's module compiler resolves these references **lazily**, at `LaurenFactory.create()` time, when every file in the application has been fully loaded:

```python
# users/module.py
from typing import ForwardRef
from lauren import module, injectable

@injectable()
class UserRepo:
    def list_users(self) -> list[str]: ...

@module(
    providers=[UserRepo],
    # No import of orders.module here — break the cycle.
    imports=[ForwardRef("OrderModule")],
    exports=[UserRepo],
)
class UserModule: ...
```

```python
# orders/module.py
from typing import ForwardRef
from lauren import module, injectable

@injectable()
class OrderService:
    def list_orders(self) -> list[str]: ...

@module(
    providers=[OrderService],
    imports=[ForwardRef("UserModule")],
    exports=[OrderService],
)
class OrderModule: ...
```

```python
# app.py
import users.module   # loads UserModule — no circular import
import orders.module  # loads OrderModule — no circular import

from lauren import LaurenFactory, module
from users.module import UserModule
from orders.module import OrderModule

@module(imports=[UserModule, OrderModule])
class AppModule: ...

app = await LaurenFactory.create(AppModule)
```

By the time `LaurenFactory.create(AppModule)` runs, both `UserModule` and `OrderModule` are in memory.  The compiler resolves `ForwardRef("OrderModule")` by scanning the loaded `sys.modules` and wires everything correctly.

!!! tip "You only need ForwardRef on one side"
    If the dependency is truly one-directional at runtime (A uses B but B never uses A), only the *weaker* side needs `ForwardRef`.  The full mutual pattern above is shown for completeness.

## Accepted forms

All three forms are equivalent:

```python
from typing import ForwardRef

@module(imports=[
    ForwardRef("OrderModule"),           # typing.ForwardRef — explicit
    "OrderModule",                       # plain string — shorthand
])
class UserModule: ...
```

| Form | Example | Recommended when |
|---|---|---|
| `ForwardRef("Name")` | `ForwardRef("OrderModule")` | You want the intent to be self-documenting |
| `"Name"` | `"OrderModule"` | You want brevity |
| Direct class | `OrderModule` | No circular dependency — the default |

## Disambiguation with dotted names

If two loaded modules each define a class called `AuthModule`, the simple name `"AuthModule"` is ambiguous and Lauren raises `ValueError` with a message explaining the conflict.  Use a dotted name to pin the lookup to a specific Python module:

```python
@module(imports=[
    ForwardRef("myapp.auth.AuthModule"),   # unambiguous
    "myapp.auth.AuthModule",               # equivalent plain-string form
])
class AppModule: ...
```

The dotted name is split on the last `.`; the left part is looked up in `sys.modules` and the right part is read as an attribute.

## Resolution order

When `LaurenFactory.create()` is called, each `ForwardRef` entry is resolved in this order:

1. **Own-module globals** — the Python module that *declares* the `@module` class is checked first.  This is the fast path for same-file references.
2. **`sys.modules` scan** — every currently-loaded Python module is scanned for a class matching the bare name.  Ambiguous matches (same name in two loaded modules) raise `ValueError`; a single unambiguous match is accepted automatically.

Both steps happen **only** at compile time — there is no overhead at request time.

## Error messages

| Situation | Exception | Message hint |
|---|---|---|
| Name not found anywhere | `ValueError` | *"references forward import … that could not be resolved"* |
| Same name in two loaded modules | `ValueError` | *"ambiguous … use a dotted name"* |
| Dotted path's parent module not loaded | `ValueError` | *"is not loaded or does not export"* |
| Invalid entry type (not class / ForwardRef / str) | `ValueError` | *"Invalid entry in @module(imports=...)"* |
| Circular dependency via ForwardRef | `CircularModuleError` | Same cycle detection as with direct references |

## Full working example

```python
# inventory/module.py
from typing import ForwardRef
from lauren import module, injectable, controller, get

@injectable()
class InventoryService:
    def __init__(self) -> None:
        self._items: list[str] = ["widget", "gadget"]

    def all(self) -> list[str]:
        return self._items


@controller("/inventory")
class InventoryController:
    def __init__(self, svc: InventoryService) -> None:
        self.svc = svc

    @get("/")
    async def list(self) -> dict:
        return {"items": self.svc.all()}


@module(
    controllers=[InventoryController],
    providers=[InventoryService],
    exports=[InventoryService],
    imports=[ForwardRef("OrderModule")],   # orders/module.py not imported here
)
class InventoryModule: ...
```

```python
# orders/module.py
from typing import ForwardRef
from lauren import module, injectable, controller, get
from lauren.extractors import Depends

@injectable()
class OrderService:
    def __init__(self) -> None:
        self._orders: list[dict] = []

    def create(self, item: str) -> dict:
        order = {"id": len(self._orders) + 1, "item": item}
        self._orders.append(order)
        return order


@controller("/orders")
class OrderController:
    def __init__(self, orders: OrderService) -> None:
        self.orders = orders

    @get("/new/{item}")
    async def new(self, item: str) -> dict:
        return self.orders.create(item)


@module(
    controllers=[OrderController],
    providers=[OrderService],
    exports=[OrderService],
    imports=[ForwardRef("InventoryModule")],  # inventory/module.py not imported here
)
class OrderModule: ...
```

```python
# main.py
import inventory.module  # noqa: F401 — ensures InventoryModule is loaded
import orders.module     # noqa: F401 — ensures OrderModule is loaded

from lauren import LaurenFactory, module
from inventory.module import InventoryModule
from orders.module import OrderModule

@module(imports=[InventoryModule, OrderModule])
class AppModule: ...

import asyncio
app = asyncio.run(LaurenFactory.create(AppModule))
```

!!! note "Import order in main.py"
    `main.py` imports both feature modules before calling `LaurenFactory.create`.
    This ensures both classes are in `sys.modules` so `ForwardRef` can find them.
    Any file that triggers the import of both modules before `create()` is called
    works equally well — this is typically your application entry point.
