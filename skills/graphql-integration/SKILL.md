---
name: graphql-integration
description: Adds a GraphQL endpoint to a Lauren application by mounting a schema execution handler inside a controller. Use when integrating Strawberry, Ariadne, or any ASGI-compatible GraphQL library, or when a single POST /graphql endpoint is needed.
---

> Use `codemap find "controller"` to locate the module where this endpoint should live.

# GraphQL Schema & Resolver Setup

Lauren has no built-in GraphQL support — instead, expose a `/graphql` controller that delegates to the schema library of your choice.

## Pattern A — Strawberry (recommended)

```python
from __future__ import annotations
import strawberry
from lauren import controller, get, post, module, Json
from lauren.types import Response, Request
from pydantic import BaseModel

@strawberry.type
class Query:
    @strawberry.field
    def hello(self) -> str:
        return "world"

schema = strawberry.Schema(Query)

class GraphQLRequest(BaseModel):
    query: str
    variables: dict = {}
    operation_name: str | None = None

@controller("/graphql")
class GraphQLController:
    @post("/")
    async def execute(self, body: Json[GraphQLRequest]) -> dict:
        result = await schema.execute(
            body.query,
            variable_values=body.variables,
            operation_name=body.operation_name,
        )
        response: dict = {}
        if result.data is not None:
            response["data"] = result.data
        if result.errors:
            response["errors"] = [{"message": str(e)} for e in result.errors]
        return response

    @get("/")
    async def playground(self) -> Response:
        # Serve GraphiQL or redirect to playground
        html = "<html><body><p>GraphQL endpoint at POST /graphql</p></body></html>"
        return Response.html(html)

@module(controllers=[GraphQLController])
class GraphQLModule:
    pass
```

## Pattern B — Ariadne

```python
from ariadne import make_executable_schema, graphql
from ariadne.asgi import GraphQL

type_defs = """
    type Query {
        hello: String!
    }
"""

def resolve_hello(obj, info):
    return "world"

schema = make_executable_schema(type_defs, {"Query": {"hello": resolve_hello}})

@controller("/graphql")
class GraphQLController:
    @post("/")
    async def execute(self, body: Json[GraphQLRequest]) -> dict:
        success, result = await graphql(
            schema,
            data={"query": body.query, "variables": body.variables},
        )
        return result

@module(controllers=[GraphQLController])
class GraphQLModule:
    pass
```

## Pattern C — Hand-rolled mock (testing / prototyping)

```python
from lauren import controller, post, module, Json
from pydantic import BaseModel

class GraphQLRequest(BaseModel):
    query: str
    variables: dict = {}

@controller("/graphql")
class GraphQLController:
    @post("/")
    async def execute(self, body: Json[GraphQLRequest]) -> dict:
        if "{ hello }" in body.query:
            return {"data": {"hello": "world"}}
        if "users" in body.query:
            return {"data": {"users": [{"id": 1, "name": "Alice"}]}}
        return {"errors": [{"message": "Unknown query"}]}

@module(controllers=[GraphQLController])
class GraphQLModule:
    pass
```

## Key points

- All three patterns share the same controller shape: `POST /graphql` accepts `{"query": "...", "variables": {}}`.
- Keep the schema object at module level (not inside the controller) so it is built once at startup.
- For subscriptions over WebSocket, use `@ws_controller("/graphql/ws")` alongside the HTTP controller.
- Add the module to your root module's `imports` list: `@module(imports=[GraphQLModule], ...)`.
