# 3. The HQ Roster

> Heroes keep scribbling assignments on napkins and losing them mid-battle. HQ needs a
> memory that outlives a single request — and exactly one source of truth, so two
> dispatchers don't send Captain Singleton to two cities at once. (He hates that.)

!!! abstract "📋 Mission briefing"
    **You'll build:** a `HeroRepository` service, injected into the controller.
    **You'll learn:**

    - [ ] What `@injectable` does and why **scope** matters
    - [ ] Constructor injection into a controller
    - [ ] `SINGLETON` vs `REQUEST` — Captain Singleton vs. the Sidekick

---

## Give HQ a memory

Right now the controller hoards heroes in its own `self._heroes` dict. That mixes two jobs —
*handling HTTP* and *storing data* — and it can't be shared with the other controllers we'll
add later. Let's extract storage into a real service.

`@injectable` turns a plain class into something Lauren's dependency-injection container can
build and hand out:

```python title="hero_hq/roster.py"
--8<-- "docs/tutorial/hero_hq/roster.py:code"
```

---

## Meet Captain Singleton

`Scope.SINGLETON` means **one** `HeroRepository` for the entire life of the app — Captain
Singleton himself. Perfect for a shared roster: whoever recruits a hero, everyone else can
see them.

The other scopes you'll meet:

| Scope | There's… | Use it for |
|---|---|---|
| `SINGLETON` | one, forever | shared state, connection pools, config |
| `REQUEST` | a fresh one per request | per-request context — *the Sidekick* |
| `TRANSIENT` | a new one every time it's asked for | cheap, stateless helpers |

The **Sidekick** (`Scope.REQUEST`) is built fresh for each incoming request and torn down
when the response goes out — ideal for things scoped to a single mission, like a per-request
correlation id. We don't need one yet, but now you know who to call.

---

## Inject it into the front desk

A controller can ask for any provider in its `__init__`, and Lauren wires it up. Notice the
controller is now blissfully ignorant of *how* heroes are stored:

```python title="hero_hq/main.py"
from lauren import Json, LaurenFactory, Path, controller, get, module, post
from lauren.exceptions import HTTPError

from .models import CreateHero, HeroOut
from .roster import HeroRepository


class HeroNotFoundError(HTTPError):
    status_code = 404
    code = "hero_not_found"


@controller("/heroes", tags=["heroes"])
class HeroController:
    def __init__(self, roster: HeroRepository) -> None:
        # Lauren sees the type hint and injects the shared HeroRepository.
        self.roster = roster

    @get("/")
    async def list_heroes(self) -> list[HeroOut]:
        return [HeroOut(**hero) for hero in self.roster.roster()]

    @get("/{id}")
    async def get_hero(self, id: Path[int]) -> HeroOut:
        hero = self.roster.get(id)
        if hero is None:
            raise HeroNotFoundError("no such hero", detail={"id": id})
        return HeroOut(**hero)

    @post("/")
    async def recruit(self, body: Json[CreateHero]) -> tuple[HeroOut, int]:
        hero = self.roster.recruit(body.name, body.power, body.wattage)
        return HeroOut(**hero), 201


@module(controllers=[HeroController], providers=[HeroRepository])
class AppModule:
    pass


app = LaurenFactory.create(AppModule)
```

The only wiring you added was `providers=[HeroRepository]` on the module and a typed
parameter on `__init__`. Lauren matches the type to the provider and constructs everything
in the right order at startup.

!!! tip "⚡ Hero Tip"
    The type hint **is** the wiring. `roster: HeroRepository` is how Lauren knows what to
    inject — no strings, no manual registration calls, no decorator on the parameter.

!!! danger "💥 Villainous Pitfall"
    Don't mutate a singleton's state from a *sync* handler without a lock. Two requests, one
    dictionary, zero supervision — that's not a race condition, that's a *team-up*, and not
    the good kind. (Our handlers are `async` and don't `await` mid-mutation, so we're safe
    here.)

---

## ✅ Checkpoint

```text
hero_hq/
├── models.py      # CreateHero, HeroOut
├── roster.py      # HeroRepository (@injectable SINGLETON)
└── main.py        # HeroController (now injected) + AppModule + app
```

**What changed:** storage moved out of the controller into an injected, shared
`HeroRepository`, and we added a `GET /heroes/` listing. The controller now does one job.

!!! example "🧪 Try it"
    Recruit a hero, then list the roster — the data persists across requests because there's
    exactly one repository:

    ```bash
    $ curl -X POST localhost:8000/heroes/ -H 'Content-Type: application/json' \
        -d '{"name":"Tide","power":"water","wattage":4200}'
    $ curl localhost:8000/heroes/
    [{"id":1,"name":"Tide","power":"water","wattage":4200}]
    ```

---

**Next:** [4. Assembling Teams →](04-assembling-teams.md) — one module is getting crowded.
Time to split HQ into teams.
**Go deeper:** [Dependency Injection — Complete Reference](../guides/dependency-injection.md) ·
[Injectables & Providers](../core-concepts/injectables.md)
