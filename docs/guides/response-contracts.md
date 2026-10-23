# Response Contracts

> A route's output is declared the same way as its input: by types. `status_code=`
> and typed results (`Ok`, `Created`, `Accepted`, `NoContent`) say which status a
> route sends, the return annotation says what the body looks like, and
> `response_validation="filter"` makes the wire honour it — a handler that returns
> an internal object (`UserInDB`, an ORM row) through a route annotated with a
> public one (`UserOut`) sends only the public fields.

Every declaration is read once in `LaurenFactory.create`. Contradictions (a 204
route with a body type, `status_code=200` on a `Created[...]` route) fail
startup, and the request path runs prebuilt closures — no reflection.

## A minimal example

<!-- docs-contract: run -->
```python
from dataclasses import dataclass

from lauren import Created, LaurenFactory, NoContent, controller, delete, get, module, post
from lauren.testing import TestClient


@dataclass
class UserOut:
    id: int
    name: str


@dataclass
class UserInDB(UserOut):
    password_hash: str


DB = {1: UserInDB(1, "ada", "bcrypt$2b$12$...")}


@controller("/users")
class UserController:
    @get("/{id}")
    async def show(self, id: int) -> UserOut:
        return DB[id]  # the internal record: the contract keeps the hash off the wire

    @post("/")
    async def create(self) -> Created[UserOut]:
        user = DB[1]
        return Created(user, location=f"/users/{user.id}")

    @delete("/{id}")
    async def remove(self, id: int) -> NoContent:
        return NoContent()


@module(controllers=[UserController])
class AppModule:
    pass


app = LaurenFactory.create(AppModule, response_validation="filter")
client = TestClient(app)

assert client.get("/users/1").json() == {"id": 1, "name": "ada"}
created = client.post("/users/")
assert (created.status_code, created.header("location")) == (201, "/users/1")
assert client.delete("/users/1").status_code == 204

responses = app.openapi()["paths"]["/users"]["post"]["responses"]
assert list(responses) == ["201"] and "Location" in responses["201"]["headers"]
```

## `status_code=`

Every verb decorator (and `@app.get(...)` on the imperative `Lauren` class)
accepts `status_code=`, an int between 100 and 599:

<!-- docs-contract: fragment -->
```python
@post("/", status_code=201)
async def create(self, body: Json[CreateUser]) -> UserOut: ...

@delete("/{id}", status_code=204)
async def remove(self, id: int) -> None: ...
```

| Handler returns | No `status_code=` | `status_code=N` |
|---|---|---|
| a value or model | 200 | **N** |
| `None` | 204, empty body | **N**, empty body (e.g. 202 "accepted, nothing to say") |
| `(body, S)` / `(body, S, headers)` | S | S — the tuple wins |
| a typed result (`Created(...)`) | the result's status | the result's status (must agree, checked at startup) |
| a `Response` | its own status | its own status |

`status_code=` is documented in OpenAPI; the tuple form is not (a status chosen
at runtime cannot be seen at startup), so prefer `status_code=` or typed results.
A 204 or 304 route may not declare a body type — anything but `None`,
`NoContent`, an absent annotation or `Response` fails startup with
`DecoratorUsageError` (`detail["reason"] == "body_not_allowed"`).

## Typed results

`lauren.Ok[T]`, `Created[T]`, `Accepted[T]` and `NoContent` put the status in the
return type, so one handler can send several statuses and the OpenAPI document
lists each of them:

<!-- docs-contract: run -->
```python
from typing import Annotated

from lauren import Accepted, Ok, Status, put


@controller("/items")
class ItemController:
    @put("/{id}")
    async def upsert(self, id: int) -> Ok[UserOut] | Created[UserOut]:
        if id in DB:
            return Ok(DB[id])
        return Created(UserOut(id, "new"), location=f"/items/{id}")

    @post("/{id}/export")
    async def export(self, id: int) -> Accepted[dict]:
        return Accepted({"job": id}, location=f"/jobs/{id}")

    @get("/{id}/head")
    async def head_chunk(self, id: int) -> Annotated[UserOut, Status(206, description="Partial")]:
        return DB[1]


@module(controllers=[ItemController])
class ItemModule:
    pass


items = TestClient(LaurenFactory.create(ItemModule))
assert items.put("/items/1").status_code == 200
assert items.put("/items/7").status_code == 201
assert items.post("/items/3/export").status_code == 202
assert items.get("/items/1/head").status_code == 206
```

* The wrapper's class picks the status; `headers=` are added to the response and
  `location=` becomes the `Location` header (`Created`, `Accepted`).
* The value is serialized with the route's encoder — `@use_encoder(...)`, else
  the app's `json_encoder` — unlike `Response.created(...)`, which uses the
  process-wide default. A `None` value sends an empty body.
* `Annotated[T, Status(code, description=...)]` gives a plain return type a
  status without a wrapper.
* A union may not declare one status twice (`Ok[A] | Ok[B]` fails startup; write
  `Ok[A | B]`). Returning a plain value from a `T | Created[T]` route uses the
  route's `status_code=` (default 200).
* Typed results also work on unannotated routes: `return Created(x)` is a
  drop-in replacement for `return x, 201`.

## Contract modes

`response_validation=` is set for the whole app on `LaurenFactory.create` (or
`Lauren(...)`) and overridden per route (`@get(..., response_validation="strict")`):

| Mode | Instance of the declared type (or a subclass) | Mapping | Other object (ORM row, SDK object) | Wrong leaf types |
|---|---|---|---|---|
| `"off"` (default) | dumped by its runtime type — **subclass fields leak** | encoded as is | `__dict__` fallback (see below) | not checked |
| `"filter"` | projected onto the declared fields | undeclared keys dropped, recursively | declared attributes read, nothing else | passed through |
| `"strict"` | projected and validated | projected and validated | read and validated | **500** |

* The declared type is `response_model=` when set, else the return annotation
  (the body type of each typed result). Routes without an annotation are not
  enforced (a `VERBOSE` startup line says so when the app enforces).
* Models emit their declared fields under their wire names — `Alias(...)`,
  `@validation_config(rename="camel")`, pydantic aliases — matching the OpenAPI
  schema.
* A missing required field, a scalar where a model or list is declared, or a value
  no union member can produce is a **contract violation** in both modes: a 500
  with `code="response_validation_error"`. The body never contains the payload;
  the violation is logged at `ERROR` with the route, handler and the structured
  issues (`{"loc": ["response", "name"], "type": "missing", ...}`), and with
  [debug pages](debugging.md) on the issues are also in the 500 body. Exception
  handlers do not see violations; interceptors see the exception raised.
* Pydantic models and `msgspec.Struct` types project through their own library.
  A value that is not an instance (a dict, an ORM row) is converted with
  `from_attributes=True`, so a value they cannot convert is a violation even in
  `filter` mode.
* An unsupported declared type (a class no validator knows) fails startup under
  `filter` / `strict` with `UnresolvableParameterError`
  (`detail["reason"] == "unsupported_response_type"`, `detail["param"] == "<return>"`).
* `StreamingResponse[T]` items are projected one by one. A first item that does
  not fit fails the request with a 500; a later one ends the stream with a
  `response_validation_error` error frame.
* Interceptors run after the contract and are trusted: one that rewrites the body
  is not re-validated. Returning a `Response` opts out of the contract.

`response_model=` with the contract off only documents a shape the wire does not
honour, so `LaurenFactory.create` logs one warning listing those routes. The
default stays `"off"` in 1.x; lauren 2.0 makes `"filter"` the default.

## Return the entity, annotate the DTO

With `filter`, a repository can hand back whatever it has — an ORM entity, a row,
an internal dataclass — and the annotation decides what leaves:

<!-- docs-contract: run -->
```python
class AccountRow:
    def __init__(self) -> None:
        self.id = 7
        self.name = "Ada"
        self.iban = "FR76 3000 6000 0112 3456 7890 189"


@controller("/accounts")
class AccountController:
    @get("/me")
    async def me(self) -> UserOut:
        return AccountRow()  # only `id` and `name` are read


@module(controllers=[AccountController])
class AccountModule:
    pass


accounts = TestClient(LaurenFactory.create(AccountModule, response_validation="filter"))
assert accounts.get("/accounts/me").json() == {"id": 7, "name": "Ada"}
```

Only declared fields are read, so a lazy ORM relationship is loaded only if the
DTO declares it.

## Declaring errors with `@raises`

`@raises(...)` records the `HTTPError` subclasses a route raises. It changes
nothing at runtime: it feeds the OpenAPI document (one response per status,
referencing the error envelope, with the error `code`s in `x-lauren-error-codes`)
and `ReflectedRoute.raises`:

<!-- docs-contract: run -->
```python
from lauren import NotFoundError, UnauthorizedError, raises


@controller("/profiles")
@raises(UnauthorizedError)  # every route of the controller
class ProfileController:
    @get("/{id}")
    @raises(NotFoundError, description="No profile with that id")
    async def show(self, id: int) -> UserOut:
        if id not in DB:
            raise NotFoundError(f"no profile {id}")
        return DB[id]


@module(controllers=[ProfileController])
class ProfileModule:
    pass


spec = LaurenFactory.create(ProfileModule).openapi()
assert sorted(spec["paths"]["/profiles/{id}"]["get"]["responses"]) == ["200", "401", "404"]
```

Stacking accumulates; a class-level `@raises` applies to the class's own routes
(a subclass controller does not inherit it). Every argument must be an
`HTTPError` subclass with an integer `status_code`. With `debug=True`, a route
that declares `@raises` logs one warning per undeclared `HTTPError` class its
handler raises — documentation drift caught in development.

`response_headers={"X-RateLimit-Remaining": "Requests left"}` documents headers
on the success response (a string description or an OpenAPI Header Object).

## The `__dict__` fallback: `json_fallback=`

An object that no encoder rule covers (not a dict, list, model, dataclass,
`msgspec.Struct`, date, enum, …) used to be sent as its public attributes —
an SDK client or settings object returned by mistake shipped its API key.
`json_fallback=` on `LaurenFactory.create` decides:

| Policy | Behaviour |
|---|---|
| `"dict"` | the historical behaviour: public, non-callable attributes |
| `"warn"` (default) | the same output, plus one `WARN` log line per type and app |
| `"error"` | `TypeError` → 500 `internal_error` |

An encoder built with `fallback=` (`StdlibJSONEncoder(fallback="error")`,
`OrjsonEncoder`, `MsgspecEncoder`, `PydanticEncoder`) pins its own policy;
otherwise it follows the app that is encoding. Outside an app the fallback stays
`"dict"`. Under `filter` / `strict` the fallback is never reached for declared
types. lauren 2.0 makes `"error"` the default.

## Comparison with FastAPI and NestJS

| Need | Lauren | FastAPI | NestJS |
|---|---|---|---|
| Success status | `status_code=` / `Created[T]` | `status_code=` | `@HttpCode(201)` |
| Several statuses from one handler | `Ok[T] \| Created[T]` | `responses=` + `JSONResponse` | `@ApiResponse` + `res.status()` |
| Drop undeclared fields | `response_validation="filter"` | `response_model` (always on) | `ClassSerializerInterceptor` + `@Exclude()` |
| Validate the output | `"strict"` (opt-in) | always | — |
| Declared errors | `@raises(NotFoundError)` | `responses={404: ...}` | `@ApiNotFoundResponse()` |

## Non-goals

* The tuple form `return body, 201` keeps working; it is just undocumented.
* Returning a `Response` (`EventStream`, `Response.file(...)`, a subclass) is
  passed through untouched — the escape hatch.
* No serialization groups or DTO factories: declare a separate output type.
* No change to the error envelope ([Error Catalog](../reference/errors.md)).

See also: [Custom Responses](custom-responses.md),
[Validation](validation.md) and the [Responses reference](../reference/responses.md).
