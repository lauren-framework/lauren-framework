# 2. The Hero Dossier

> A hero shows up wanting to join. They claim a power level of "over nine thousand."
> HQ needs paperwork — and someone to check it. Enter the Auditor.

!!! abstract "📋 Mission briefing"
    **You'll build:** `POST /heroes/` to recruit a hero and `GET /heroes/{id}` to fetch one.
    **You'll learn:**

    - [ ] Typed request bodies with `Json[Model]` (and automatic validation)
    - [ ] Typed path parameters with `Path[int]`
    - [ ] Returning models and `(model, status)` tuples
    - [ ] Custom error responses with a stable envelope

---

## Write the paperwork

A hero's application — and what HQ files in return — are just Pydantic models:

```python title="hero_hq/models.py"
--8<-- "docs/tutorial/hero_hq/models.py:code"
```

---

## Take applications

Now teach the front desk to accept applications and look heroes up. Two new ideas:

- `body: Json[CreateHero]` reads the JSON request body and validates it against the model.
- `id: Path[int]` pulls `{id}` out of the URL and parses it as an `int`.

```python title="hero_hq/main.py"
from lauren import Json, LaurenFactory, Path, controller, get, module, post
from lauren.exceptions import HTTPError

from .models import CreateHero, HeroOut


class HeroNotFoundError(HTTPError):
    status_code = 404
    code = "hero_not_found"


@controller("/heroes", tags=["heroes"])
class HeroController:
    def __init__(self) -> None:
        # A temporary home for heroes. (@controller is a singleton, so this
        # dict survives between requests — we'll do this properly in step 3.)
        self._heroes: dict[int, dict] = {}
        self._next_id = 1

    @post("/")
    async def recruit(self, body: Json[CreateHero]) -> tuple[HeroOut, int]:
        hero = {"id": self._next_id, "name": body.name, "power": body.power, "wattage": body.wattage}
        self._heroes[self._next_id] = hero
        self._next_id += 1
        return HeroOut(**hero), 201

    @get("/{id}")
    async def get_hero(self, id: Path[int]) -> HeroOut:
        hero = self._heroes.get(id)
        if hero is None:
            raise HeroNotFoundError("no such hero", detail={"id": id})
        return HeroOut(**hero)


@module(controllers=[HeroController])
class AppModule:
    pass


app = LaurenFactory.create(AppModule)
```

What to notice:

- `recruit` returns `(HeroOut, 201)` — Lauren builds a `201 Created` response with the model
  as JSON. (You can also return just the model, or `(body, status, headers)`.)
- `get_hero` raises a `HeroNotFoundError`. Because it subclasses `HTTPError` with a
  `status_code` and a stable `code`, Lauren renders it as a structured error envelope.

---

## Meet the Auditor

You didn't write a single line of validation, but you have it. `Json[CreateHero]` runs the
body through Pydantic; anything that doesn't fit is rejected with a `422` before your handler
ever runs. The Auditor is thorough and entirely unpaid.

!!! example "🧪 Try it"
    ```bash
    # A legitimate hero:
    $ curl -X POST localhost:8000/heroes/ \
        -H 'Content-Type: application/json' \
        -d '{"name":"Volt","power":"lightning","wattage":9001}'
    {"id":1,"name":"Volt","power":"lightning","wattage":9001}

    # "over nine thousand" is not an int. The Auditor is unimpressed:
    $ curl -i -X POST localhost:8000/heroes/ \
        -H 'Content-Type: application/json' \
        -d '{"name":"Volt","power":"lightning","wattage":"over nine thousand"}'
    HTTP/1.1 422 Unprocessable Entity

    # Asking for a hero who doesn't exist — your custom envelope:
    $ curl -i localhost:8000/heroes/999
    HTTP/1.1 404 Not Found
    {"error":{"code":"hero_not_found","message":"no such hero","detail":{"id":999}}}
    ```

!!! danger "💥 Villainous Pitfall"
    Don't reach for `Path[int]` *and* hand-write `int(id)` in the body of your handler.
    The extractor already parsed and validated it — doing it twice is how you end up with
    two sources of truth and one very confused on-call engineer.

---

## ✅ Checkpoint

```text
hero_hq/
├── models.py      # CreateHero, HeroOut
└── main.py        # HeroController (recruit + get_hero) + AppModule + app
```

**What changed:** the front desk now recruits heroes, validates their paperwork, fetches
them by id, and returns a clean error envelope when one is missing.

---

**Next:** [3. The HQ Roster →](03-the-roster.md) — that `self._heroes` dict is doing a job
that deserves its own class. Time for dependency injection.
**Go deeper:** [Implicit Parameter Extraction](../guides/implicit-params.md) ·
[Custom Exception Handlers](../guides/custom-exception-handlers.md)
