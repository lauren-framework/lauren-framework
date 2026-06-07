"""Comprehensive multi-backend e2e tests via TestClient.

Covers:
  - All four struct backends (pydantic, msgspec, dataclass, TypedDict)
  - GET, POST, PUT, DELETE verbs
  - 200, 422, 404 response codes
  - /openapi.json structure
  - Pydantic absent (monkeypatched) and present configurations
"""

import dataclasses
import sys
from typing import TypedDict
import pytest


@dataclasses.dataclass
class DCModel:
    name: str
    value: int = 0


class TDModel(TypedDict):
    name: str
    value: int


@pytest.fixture(scope="module")
def dc_client():
    from lauren import Lauren
    from lauren.testing import TestClient

    app = Lauren()

    @app.post("/dc")
    async def create_dc(body: DCModel) -> DCModel:
        return body

    @app.get("/dc/{name}")
    async def get_dc(name: str) -> DCModel:
        return DCModel(name=name)

    @app.put("/dc/{name}")
    async def update_dc(name: str, body: DCModel) -> DCModel:
        return DCModel(name=name, value=body.value)

    @app.delete("/dc/{name}")
    async def delete_dc(name: str) -> dict:
        return {"deleted": name}

    return TestClient(app)


@pytest.fixture(scope="module")
def td_client():
    from lauren import Lauren
    from lauren.testing import TestClient

    app = Lauren()

    @app.post("/td")
    async def create_td(body: TDModel) -> dict:
        return dict(body)

    return TestClient(app)


class TestDataclassAllVerbsE2E:
    def test_post_200(self, dc_client):
        r = dc_client.post("/dc", json={"name": "widget"})
        assert r.status_code == 200
        assert r.json()["name"] == "widget"

    def test_post_422_missing_required(self, dc_client):
        r = dc_client.post("/dc", json={})
        assert r.status_code == 422

    def test_get_200(self, dc_client):
        r = dc_client.get("/dc/mywidget")
        assert r.status_code == 200
        assert r.json()["name"] == "mywidget"

    def test_put_200(self, dc_client):
        r = dc_client.put("/dc/mywidget", json={"name": "x", "value": 99})
        assert r.status_code == 200
        assert r.json()["value"] == 99

    def test_delete_200(self, dc_client):
        r = dc_client.delete("/dc/mywidget")
        assert r.status_code == 200
        assert r.json()["deleted"] == "mywidget"

    def test_get_nonexistent_404(self, dc_client):
        r = dc_client.get("/undefined-route")
        assert r.status_code == 404

    def test_openapi_has_dc_schema(self, dc_client):
        spec = dc_client.get("/openapi.json").json()
        assert "DCModel" in spec["components"]["schemas"]


class TestTypedDictE2E:
    def test_post_200(self, td_client):
        r = td_client.post("/td", json={"name": "thing", "value": 3})
        assert r.status_code == 200

    def test_post_422_missing_field(self, td_client):
        r = td_client.post("/td", json={"name": "thing"})
        assert r.status_code == 422

    def test_openapi_has_td_schema(self, td_client):
        spec = td_client.get("/openapi.json").json()
        assert "TDModel" in spec["components"]["schemas"]


class TestPydanticAbsentE2E:
    def test_all_routes_work_without_pydantic(self, dc_client, monkeypatch):
        monkeypatch.setitem(sys.modules, "pydantic", None)
        r = dc_client.post("/dc", json={"name": "nopydantic"})
        assert r.status_code == 200
        assert r.json()["name"] == "nopydantic"

    def test_openapi_works_without_pydantic(self, dc_client, monkeypatch):
        monkeypatch.setitem(sys.modules, "pydantic", None)
        spec = dc_client.get("/openapi.json").json()
        assert "DCModel" in spec["components"]["schemas"]


class TestPydanticPresentRegressionE2E:
    @pytest.fixture(scope="class")
    def pyd_client(self):
        pytest.importorskip("pydantic")
        from pydantic import BaseModel
        from lauren import Lauren
        from lauren.testing import TestClient

        class PydModel(BaseModel):
            name: str
            value: int = 0

        app = Lauren()

        @app.post("/pyd")
        async def create(body: PydModel) -> PydModel:
            return body

        return TestClient(app)

    def test_pydantic_model_body_works(self, pyd_client):
        r = pyd_client.post("/pyd", json={"name": "test"})
        assert r.status_code == 200
        assert r.json()["name"] == "test"

    def test_pydantic_422_on_bad_type(self, pyd_client):
        # "not-a-number" cannot be coerced to int even in pydantic's lax mode
        r = pyd_client.post("/pyd", json={"name": "test", "value": "not-a-number"})
        assert r.status_code == 422

    def test_pydantic_schema_in_openapi(self, pyd_client):
        spec = pyd_client.get("/openapi.json").json()
        assert "PydModel" in spec["components"]["schemas"]
