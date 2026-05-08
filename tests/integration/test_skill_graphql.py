"""Integration tests for skill 22: GraphQL Schema & Resolver Setup."""

from __future__ import annotations

from pydantic import BaseModel

from lauren import LaurenFactory, Json, controller, module, post
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class GraphQLRequest(BaseModel):
    query: str
    variables: dict = {}


# ---------------------------------------------------------------------------
# Controller — hand-rolled mock resolver
# ---------------------------------------------------------------------------


@controller("/graphql")
class GraphQLController:
    @post("/")
    async def execute(self, body: Json[GraphQLRequest]) -> dict:
        if "{ hello }" in body.query:
            return {"data": {"hello": "world"}}
        if "users" in body.query:
            return {
                "data": {
                    "users": [
                        {"id": 1, "name": "Alice"},
                        {"id": 2, "name": "Bob"},
                    ]
                }
            }
        if "createUser" in body.query:
            name = body.variables.get("name", "Unknown")
            return {"data": {"createUser": {"id": 99, "name": name}}}
        return {"errors": [{"message": "Unknown query"}]}


@module(controllers=[GraphQLController])
class GraphQLModule:
    pass


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(GraphQLModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGraphQLEndpoint:
    def test_hello_query(self) -> None:
        client = build_app()
        r = client.post("/graphql/", json={"query": "{ hello }"})
        assert r.status_code == 200
        assert r.json() == {"data": {"hello": "world"}}

    def test_users_query(self) -> None:
        client = build_app()
        r = client.post("/graphql/", json={"query": "{ users { id name } }"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data["users"]) == 2
        assert data["users"][0]["name"] == "Alice"

    def test_mutation_with_variables(self) -> None:
        client = build_app()
        r = client.post(
            "/graphql/",
            json={
                "query": "mutation CreateUser($name: String!) { createUser(name: $name) { id name } }",
                "variables": {"name": "Charlie"},
            },
        )
        assert r.status_code == 200
        assert r.json()["data"]["createUser"]["name"] == "Charlie"

    def test_unknown_query_returns_errors(self) -> None:
        client = build_app()
        r = client.post("/graphql/", json={"query": "{ unknownField }"})
        assert r.status_code == 200
        body = r.json()
        assert "errors" in body
        assert body["errors"][0]["message"] == "Unknown query"

    def test_missing_query_field_returns_422(self) -> None:
        client = build_app()
        r = client.post("/graphql/", json={"variables": {}})
        assert r.status_code == 422

    def test_default_variables_is_empty_dict(self) -> None:
        client = build_app()
        r = client.post("/graphql/", json={"query": "{ hello }"})
        assert r.status_code == 200

    def test_variables_passed_to_resolver(self) -> None:
        client = build_app()
        r = client.post(
            "/graphql/",
            json={"query": "mutation { createUser }", "variables": {"name": "Dave"}},
        )
        assert r.status_code == 200
        assert r.json()["data"]["createUser"]["name"] == "Dave"
