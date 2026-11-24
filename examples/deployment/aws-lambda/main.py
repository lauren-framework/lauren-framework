from __future__ import annotations

from lauren import LaurenFactory, controller, get, module
from mangum import Mangum


@controller("/")
class HelloController:
    @get("/")
    async def hello(self) -> dict[str, str]:
        return {"message": "Hello from Lauren"}


@module(controllers=[HelloController])
class AppModule:
    pass


app = LaurenFactory.create(AppModule)

# API Gateway HTTP API / REST API events are translated into ASGI scopes.
handler = Mangum(app)
