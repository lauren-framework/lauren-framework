# Lauren Module System — Reference

## Contents
- [@module decorator signature](#module-decorator-signature)
- [Provider visibility rules](#provider-visibility-rules)
- [Sharing providers across modules](#sharing-providers-across-modules)
- [Dynamic module pattern](#dynamic-module-pattern)
- [Module errors](#module-errors)

---

## @module decorator signature

```python
from lauren import module

@module(
    controllers=[ControllerA, ControllerB],  # classes decorated with @controller
    providers=[ServiceA, ServiceB],          # classes decorated with @injectable
    imports=[OtherModule],                   # @module classes
    exports=[ServiceA],                      # subset of providers or re-exports
)
class MyModule:
    pass
```

All arguments are optional. An empty root module is valid but useless.

## Provider visibility rules

```
ModuleA (providers=[X])  ─imports→  ModuleB (providers=[Y])
                                     (exports=[Y])

Inside ModuleA: X ✓, Y ✓ (imported)
Inside ModuleB: Y ✓
Inside a module that imports ModuleA: X ✗ (not exported)
```

Key rule: imports do NOT propagate transitively without explicit re-exports.

```python
# ModuleA imports ModuleB which imports ModuleC
# ModuleA can NOT use ModuleC's exports unless ModuleB re-exports them

@module(imports=[ModuleC], exports=[ServiceC])  # explicit re-export
class ModuleB: ...

@module(imports=[ModuleB])  # now ServiceC is visible here
class ModuleA: ...
```

## Sharing providers across modules

Pattern: a `SharedModule` that exports common providers.

```python
from lauren import module, injectable, Scope

@injectable(scope=Scope.SINGLETON)
class ConfigService:
    database_url: str = "postgresql://..."

@injectable(scope=Scope.SINGLETON)
class LogService:
    pass

@module(providers=[ConfigService, LogService], exports=[ConfigService, LogService])
class SharedModule:
    pass

# Any module that imports SharedModule can inject ConfigService and LogService
@module(imports=[SharedModule], controllers=[UsersController], providers=[UsersService])
class UsersModule:
    pass
```

## Dynamic module pattern

For modules that need runtime configuration (e.g. database URL from env):

```python
import os
from lauren import module, injectable, Scope

@injectable(scope=Scope.SINGLETON)
class DatabaseService:
    def __init__(self) -> None:
        self._url = os.environ["DATABASE_URL"]

@module(providers=[DatabaseService], exports=[DatabaseService])
class DatabaseModule:
    pass
```

For true parameterised modules, use a factory function:

```python
def make_database_module(url: str):
    @injectable(scope=Scope.SINGLETON)
    class Db:
        def __init__(self) -> None:
            self._url = url

    @module(providers=[Db], exports=[Db])
    class DatabaseModule:
        pass

    return DatabaseModule
```

## Module errors

| Error | Cause |
|---|---|
| `CircularModuleError` | A → B → A import cycle |
| `ModuleExportViolation` | Exported class not in providers or imports |
| `MissingProviderError` | Injectable depends on class not registered anywhere |
| `CircularDependencyError` | A → B → A DI dependency cycle |
| `DIScopeViolationError` | SINGLETON depends on REQUEST-scoped provider |
