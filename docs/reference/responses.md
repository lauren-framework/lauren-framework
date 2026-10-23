# Responses

Typed results, the `Status` marker and `@raises` — the response-contract surface.
See the [Response Contracts guide](../guides/response-contracts.md) for the full
story (`status_code=`, `response_validation=`, `json_fallback=`).

```python
from lauren import Ok, Created, Accepted, NoContent, Status, raises
```

## Typed results

::: lauren.Ok

::: lauren.Created

::: lauren.Accepted

::: lauren.NoContent

## Status marker

::: lauren.Status

## Declared errors

::: lauren.raises

::: lauren.decorators.RaisesSpec

## Reflection

`ReflectedRoute.status_code`, `.responses_typed` (a tuple of
[`ReflectedResponse`](reflect.md#reflectedresponse)) and `.raises` expose the
declared contract of a route; see the [Reflect reference](reflect.md).
