# Validation

> Lauren validates request data itself. Dataclasses, `TypedDict`, `NamedTuple`,
> enums, literals, numbers, dates, UUIDs and containers are checked against
> their annotations — nested, with defaults and `__post_init__` — without
> pydantic. Pydantic models and `msgspec.Struct` types keep their own
> validators. Every failure, from every backend and transport, becomes the same
> `{loc, type, msg}` issue, and a request with several bad parameters gets
> one 422 that lists them all.

Validation is compiled once in `LaurenFactory.create`: every extractor type,
WebSocket payload and stream item type is turned into a validator by the app's
type compiler, so requests run prebuilt checks with no reflection.

## A pydantic-free example

<!-- docs-contract: run -->
```python
from dataclasses import dataclass, field
from typing import Annotated

from lauren import Json, LaurenFactory, controller, module, post
from lauren.testing import TestClient
from lauren.validation import Ge, Le, MaxLen, Pattern, ValidationPolicy, validation_config


@dataclass
class Address:
    city: Annotated[str, MaxLen(80)]
    zip: Annotated[str, Pattern(r"^\d{5}$")]


@validation_config(extra="forbid")
@dataclass
class CreateUser:
    email: Annotated[str, Pattern(r"^[^@\s]+@[^@\s]+$"), MaxLen(254)]
    age: Annotated[int, Ge(13), Le(130)]
    tags: Annotated[list[str], MaxLen(10)] = field(default_factory=list)
    address: Address | None = None

    def __post_init__(self) -> None:
        if "admin" in self.tags:
            raise ValueError("tag 'admin' is reserved")


@controller("/users")
class UserController:
    @post("/")
    async def create(self, body: Json[CreateUser]) -> dict:
        # body.age is an int and body.address an Address instance
        return {"age": body.age, "city": body.address.city if body.address else None}


@module(controllers=[UserController])
class AppModule:
    pass


app = LaurenFactory.create(AppModule, validation=ValidationPolicy(stdlib_models="enforce"))
client = TestClient(app)

ok = client.post("/users/", json={"email": "a@b.co", "age": "30", "address": {"city": "Lyon", "zip": "69001"}})
assert ok.json() == {"age": 30, "city": "Lyon"}

bad = client.post("/users/", json={"email": "nope", "age": 7, "admin": True})
assert bad.status_code == 422
assert [(i["loc"], i["type"]) for i in bad.json()["error"]["detail"]["issues"]] == [
    (["body", "email"], "string_pattern_mismatch"),
    (["body", "age"], "greater_than_equal"),
    (["body", "admin"], "extra_forbidden"),
]

reserved = client.post("/users/", json={"email": "a@b.co", "age": 30, "tags": ["admin"]})
assert reserved.json()["error"]["detail"]["issues"][0] == {
    "loc": ["body"],
    "type": "value_error",
    "msg": "tag 'admin' is reserved",
    "ctx": {"error": "tag 'admin' is reserved"},
}
```

`ValidationPolicy(stdlib_models="enforce")` makes the native results count for
every stdlib model. In 1.x the default is `"warn"` (see
[Policy and migration](#policy-and-migration)); a type that uses validation
markers or `@validation_config`, like `CreateUser` above, is enforced in every
mode because it was written for the engine.

## Supported types and coercion

Path, query, header, cookie and urlencoded-form values arrive as strings and
are always parsed laxly. JSON bodies, WebSocket frames and stream items use the
policy's `json_mode`: `"lax"` (the default, like pydantic) or `"strict"`.

| Target | Strict accepts | Lax also accepts | Issue `type`s |
|---|---|---|---|
| `str` | `str` | — | `string_type` |
| `int` | `int` (not `bool`) | integer strings (`"12"`), integral floats (`12.0`) | `int_type`, `int_parsing`, `int_from_float` |
| `float` | `int`, `float` | numeric strings | `float_type`, `float_parsing` |
| `bool` | `bool` | `true/false`, `1/0`, `yes/no`, `on/off` (any case), `0`/`1` | `bool_type`, `bool_parsing` (`"maybe"` is rejected) |
| `Decimal` | `int`, numeric `str` | `float` | `decimal_type`, `decimal_parsing`, `finite_number` |
| `UUID` | canonical `str` | any case, braces, `urn:uuid:` | `uuid_type`, `uuid_parsing` |
| `datetime` / `date` / `time` | ISO 8601 `str` | — (no Unix timestamps) | `datetime_parsing`, `date_parsing`, `time_parsing` |
| `timedelta` | ISO 8601 duration (`PT5M`) | seconds (number or numeric string) | `time_delta_type`, `time_delta_parsing` |
| `bytes` | `str` (UTF-8) | — | `bytes_type` |
| `Enum` | a member value | the string form of a value (`"2"` for `IntEnum`) | `enum` |
| `Literal[...]` | the exact value | the string form of `int` / `bool` literals | `literal_error` |
| `list` / `tuple` / `set` / `frozenset` | JSON array | query and form: repeated keys (`?tag=a&tag=b`) | `list_type`, `tuple_type`, `set_type`, `too_short`, `too_long` |
| `dict[K, V]` | JSON object | — | `dict_type` |
| dataclass / `TypedDict` / `NamedTuple` | JSON object (`NamedTuple` also an array) | query or form: one key per field | `model_type`, `missing`, `extra_forbidden` |
| `X \| None` | `null` or `X` | an empty query/form value is `None` when `X` is not `str` | as for `X` |
| `A \| B` | exact type match first, then each member strictly, then laxly | query values: each member laxly, left to right (`int \| str` reads `"5"` as `5`) | every member's issues, under the member name |
| `Discriminated[A \| B, "key"]` | the tag picks the variant | — | `union_tag_not_found`, `union_tag_invalid`, then the variant's issues under the tag |
| `Any` / `object` | anything | anything | — |

`complex`, `pathlib` paths and `UploadFile` (in form models) are accepted too.
Path, header and cookie values have no repeated keys, so `Path[list[int]]` and
`Header[list[str]]` split one value on commas.

Construction semantics:

* Dataclasses are built by calling the class, so `__init__` and `__post_init__`
  run; `field(init=False)` is skipped, `InitVar` and `kw_only` work, frozen
  classes work. A `ValueError`, `TypeError` or `AssertionError` raised by
  `__post_init__` becomes a `value_error` issue at the model's location.
* An instance of the target dataclass or `NamedTuple` is accepted as it is.
* `TypedDict` values are new dicts holding only the declared keys;
  `Required` / `NotRequired` / `total=False` are honoured.
* Recursive types (`children: list[Node]`) work.
* A field type the engine does not know (an arbitrary class) is reported at
  startup: in `"enforce"` mode as `UnresolvableParameterError`, otherwise as a
  warning, and that parameter keeps the pre-engine behaviour.

## Constraints

Constraint markers ride in `Annotated` metadata, on model fields and on
handler parameters alike:

| Marker | Applies to | Issue `type` (`ctx`) |
|---|---|---|
| `Ge(n)`, `Gt(n)`, `Le(n)`, `Lt(n)` | `int`, `float`, `Decimal` | `greater_than_equal` (`{"ge": n}`), `greater_than`, `less_than_equal`, `less_than` |
| `MultipleOf(n)` | `int`, `float`, `Decimal` | `multiple_of` |
| `MinLen(n)`, `MaxLen(n)` | `str`, `bytes`, collections, mappings | `string_too_short` / `string_too_long`, `bytes_too_short` / `bytes_too_long`, `too_short` / `too_long` |
| `Pattern(regex)` | `str` (`re.search` semantics) | `string_pattern_mismatch` (`{"pattern": ...}`) |
| `Strict()` | any field | the field is validated strictly |
| `Alias("wireName")` | a model field or parameter | read and written as `wireName` |

<!-- docs-contract: fragment -->
```python
@get("/search")
async def search(self, q: Query[str, MinLen(2)], size: Annotated[int, Le(100)] = 10) -> dict: ...
```

Any `Annotated` metadata object exposing the
[annotated-types](https://github.com/annotated-types/annotated-types) attribute
names (`ge`, `gt`, `le`, `lt`, `multiple_of`, `min_length`, `max_length`,
`pattern`) is honoured, so `annotated_types.Ge(13)` and
`annotated_types.Len(1, 10)` work unchanged, and pydantic reads the same
metadata. `QueryField(ge=..., le=...)` and the other field descriptors are
lowered onto the same checks.

Constraints are checked at startup: a constraint that does not apply to the
type (`Ge` on a `str`), an invalid regex, `MinLen` greater than `MaxLen` or an
unsupported type raise `UnresolvableParameterError` with
`detail = {"class", "handler", "param", "type", "reason", "field"?}` and
`reason` one of `"bad_constraint"`, `"bad_pattern"`, `"unsupported_type"`.

## Per-type settings: `@validation_config`

<!-- docs-contract: run -->
```python
from dataclasses import dataclass

from lauren.validation import TypeCompiler, validation_config


@validation_config(extra="forbid", rename="camel")
@dataclass
class Profile:
    first_name: str
    last_seen_at: str = ""


profile = TypeCompiler().compile(Profile).validate({"firstName": "Ada", "lastSeenAt": "now"})
assert profile.first_name == "Ada"
```

* `extra="ignore"` (default) drops unknown keys, `"forbid"` reports each one
  as `extra_forbidden`. The app-wide default is `ValidationPolicy(extra=...)`.
* `strict=True` validates every field strictly.
* `rename="camel"` maps `snake_case` fields to `camelCase` on the wire; an
  `Alias` on a field wins.

The decorator returns the class unchanged. Subclasses do not inherit it
(re-decorate them), stacking merges the settings, and bare
`@validation_config` raises `DecoratorUsageError`. Query and form models read
only their declared keys, so `extra` applies to bodies, frames and items.

## The error payload

A validation failure is a 422 `ExtractorError` whose `detail.issues` lists
every problem:

```json
{"error": {"code": "extractor_error", "message": "2 validation errors",
  "detail": {
    "field": "page",
    "errors": ["query.page: Input should be a valid integer, unable to parse string as an integer",
               "query.size: Input should be less than or equal to 100"],
    "issues": [
      {"loc": ["query", "page"], "type": "int_parsing",
       "msg": "Input should be a valid integer, unable to parse string as an integer"},
      {"loc": ["query", "size"], "type": "less_than_equal",
       "msg": "Input should be less than or equal to 100", "ctx": {"le": 100}}]}}}
```

* `loc` starts with the source and the wire name: `["path", "id"]`,
  `["query", "page"]`, `["header", "x-trace"]`, `["cookie", "sid"]`,
  `["body"]` or `["body", "address", "zip"]`, `["form", "email"]`,
  `["frame", "data", "age"]` for WebSocket frames and `["item", 3, "text"]`
  for `Stream[T]` items.
* `type` is a stable code (pydantic-core's vocabulary where they overlap);
  `msg` is English text; `ctx` carries the constraint values.
* `input` (the rejected value) is included only when `debug` is on or
  `ValidationPolicy(include_input=True)`; it is never logged.
* A single failing parameter keeps the code, message and `detail` keys it had
  in earlier releases (`extractor_field_error` for path, query, header and
  cookie values) with `issues` added. `detail.field` and the `detail.errors`
  strings are kept until 2.0 — read `detail.issues`.
* With `error_format="rfc9457"` the issues become the problem document's
  `errors` array (`{"detail", "pointer" | "in"/"parameter", "code"}`); with
  `"rfc7807"` they ride in the `errors` member as `errors.issues`.

Mapping the issues onto a form in the browser:

```javascript
const res = await fetch("/users/", {method: "POST", body: JSON.stringify(form)});
if (res.status === 422) {
  const {error} = await res.json();
  for (const issue of error.detail.issues) {
    const [source, ...path] = issue.loc;          // "body", "address", "zip"
    showError(path.join("."), issue.msg, issue.type);
  }
}
```

Pydantic errors are translated field for field (their `url` is dropped),
msgspec messages such as ``Expected `int`, got `str` - at `$.age` `` are
parsed for the path and the expected type (unrecognised ones become
`value_error` with the original text), and `Discriminated` tag errors gain a
`type` and a `loc`. A `msgspec.Struct` body is decoded and validated in one
`msgspec.json.decode` call.

## Every bad parameter in one response

The extraction loop collects the failures of every parameter before
answering, up to `ValidationPolicy(max_issues=50)`:

<!-- docs-contract: run -->
```python
from lauren import Header, LaurenFactory, Query, QueryField, controller, get, module
from lauren.testing import TestClient


@controller("/v")
class PageController:
    @get("/items/{item_id}")
    async def items(self, item_id: int, page: Query[int], x_count: Header[int], size: int = QueryField(default=10, le=100)) -> dict:
        return {}


@module(controllers=[PageController])
class PageModule:
    pass


client = TestClient(LaurenFactory.create(PageModule))
detail = client.get("/v/items/abc?page=x&size=1000", headers={"x-count": "many"}).json()["error"]["detail"]
assert [issue["loc"] for issue in detail["issues"]] == [
    ["path", "item_id"], ["query", "page"], ["header", "x-count"], ["query", "size"],
]
```

Errors that are not validation failures still answer immediately: a custom
extractor raising `UnauthorizedError` gets its 401, and an oversized body its
413. Pipes run only on values that extracted successfully; a pipe failure is a
`value_error` issue at the parameter's location.

## Policy and migration

```python
LaurenFactory.create(
    AppModule,
    validation=ValidationPolicy(
        stdlib_models="warn",   # "legacy" | "warn" (1.x default) | "enforce" (2.0 default)
        json_mode="lax",        # "lax" | "strict"
        extra="ignore",         # default for stdlib models; @validation_config wins
        include_input=None,     # None: echo "input" only when debug is on
        max_issues=50,          # cap on reported issues per request
    ),
)
```

| `stdlib_models` | Dataclass / TypedDict / NamedTuple bodies, query and form models, typed scalars |
|---|---|
| `"legacy"` | The shallow pre-engine behaviour (escape hatch) |
| `"warn"` | The native validator runs; a request it would reject logs **one warning per route and issue type** (never the input) and continues with the pre-engine value, so responses do not change |
| `"enforce"` | Native results are used — nested models are real instances, `?on=maybe` is a 422, an empty `?n=` is `None` for `int \| None`, `Path[UUID]` is a `UUID` — and failures answer 422 |

Pydantic models and `msgspec.Struct` types are validated by their own library
in every mode; structured `issues`, collect-all and WebSocket/HTTP parity also
apply in every mode, because they only change requests that already fail.
Under `"warn"`, `__post_init__` of a dataclass body may run twice per request
(once for the shadow check). The plan for 2.0 is to make `"enforce"` the
default; turn it on now and fix what the warnings report.

`Lauren(validation=...)` forwards the same policy.

## WebSocket frames and streams

A `@on_message` handler with `body: Json[T]` and a `Stream[T]` parameter use
the same compiled validator as an HTTP body of type `T`, so one payload gives
the same issues on every transport, with or without pydantic installed. A bad
frame answers with an error frame whose `detail` is
`{"event", "issues", "errors"}` (locations rooted at `["frame", "data"]`);
a bad stream item raises a 422 before the first byte is sent, or a trailing
error frame once the stream is flowing.

## Outside requests: `TypeCompiler` and `validate`

<!-- docs-contract: run -->
```python
from dataclasses import dataclass

from lauren.validation import TypeCompiler, ValidationFailed, validate


@dataclass
class Job:
    name: str
    retries: int = 3


compiler = TypeCompiler()          # hold one for hot paths; compiled types are cached
job_type = compiler.compile(Job)
job = job_type.validate({"name": "sync", "retries": "5"})
assert job.retries == 5
assert job_type.project(job) == {"name": "sync", "retries": 5}

try:
    validate(Job, {"retries": "x"})  # convenience: compiles on every call
except ValidationFailed as exc:
    assert [i.type for i in exc.issues] == ["missing", "int_parsing"]
```

Inside an app, `app.type_compiler` is the compiler `LaurenFactory.create`
used, so services, commands and job payloads get exactly the semantics of the
HTTP layer. A `CompiledType` has three operations:

* `validate(value, *, loc=())` — the validated value, or `ValidationFailed`
  (a `ValueError`) with `.issues`;
* `project(value)` — JSON-compatible builtins holding only the declared
  fields, under their wire names (extra attributes are dropped), so
  `validate(project(x))` round-trips;
* `json_schema(dialect="2020-12" | "openapi-3.1", components=None)` — a JSON
  Schema 2020-12 document, or an OpenAPI 3.1 schema whose named definitions
  are added to `components`.

## Which validator runs?

| | pydantic `BaseModel` | `msgspec.Struct` | stdlib types (native) |
|---|---|---|---|
| Selected when | the type is a pydantic model, pydantic dataclass or `Field(discriminator=)` union | the type is a `Struct` | everything else, even when pydantic is installed |
| Strict / lax | the model's own configuration | strict for bodies and frames, lax for query values | `json_mode`, `Strict()`, `@validation_config(strict=)` |
| Constraints | pydantic's (`Field(ge=...)`, annotated-types) | `msgspec.Meta` | `lauren.validation` markers or annotated-types |
| Model-level logic | validators, computed fields | `__post_init__` | `__post_init__` |
| Errors | translated to issues | translated to issues | native issues |
