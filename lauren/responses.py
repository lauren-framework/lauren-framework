"""Typed results: put a route's success status in its return type.

A handler that returns one of these wrappers tells lauren which status to
send, and the return annotation tells the startup compiler (and the OpenAPI
document) which statuses a route can produce::

    from lauren import Created, NoContent, Ok, controller, delete, post, put

    @controller("/users")
    class UserController:
        @post("/")
        async def create(self, body: Json[CreateUser]) -> Created[UserOut]:
            user = await self.repo.create(body)
            return Created(user, location=f"/users/{user.id}")

        @put("/{id}")
        async def upsert(self, id: int, body: Json[UserIn]) -> Ok[UserOut] | Created[UserOut]:
            user, created = await self.repo.upsert(id, body)
            return Created(user, location=f"/users/{id}") if created else Ok(user)

        @delete("/{id}")
        async def remove(self, id: int) -> NoContent:
            await self.repo.delete(id)
            return NoContent()

The wrapper's class decides the status; ``value`` is serialized with the
route's encoder (``@use_encoder`` → app ``json_encoder``) and, when the route
enforces a response contract (``response_validation="filter"`` / ``"strict"``),
projected onto the declared type first. ``headers`` are added to the response
and ``location`` becomes the ``Location`` header. A ``None`` value sends an
empty body with the wrapper's status.

:class:`Status` is not a return value: it is ``Annotated`` metadata that gives a
plain return type a status (``-> Annotated[Chunk, Status(206)]``).

The wrappers are plain frozen dataclasses with no framework state, so they are
safe to build anywhere (services, tests).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

T = TypeVar("T")

__all__ = ["Accepted", "Created", "NoContent", "Ok", "Status"]


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    """``200 OK`` with ``value`` as the body."""

    value: T
    headers: Mapping[str, str] | None = None
    #: The status this wrapper sends.
    status: ClassVar[int] = 200


@dataclass(frozen=True, slots=True)
class Created(Generic[T]):
    """``201 Created`` with ``value`` as the body; ``location`` becomes the ``Location`` header."""

    value: T
    location: str | None = None
    headers: Mapping[str, str] | None = None
    #: The status this wrapper sends.
    status: ClassVar[int] = 201


@dataclass(frozen=True, slots=True)
class Accepted(Generic[T]):
    """``202 Accepted``: the work was queued; ``value`` (optional) describes it, ``location`` monitors it."""

    value: T | None = None
    location: str | None = None
    headers: Mapping[str, str] | None = None
    #: The status this wrapper sends.
    status: ClassVar[int] = 202


@dataclass(frozen=True, slots=True)
class NoContent:
    """``204 No Content``: an empty body (``headers`` are still sent)."""

    headers: Mapping[str, str] | None = None
    #: The status this wrapper sends.
    status: ClassVar[int] = 204


@dataclass(frozen=True, slots=True)
class Status:
    """``Annotated`` metadata giving a plain return type a status: ``Annotated[Chunk, Status(206)]``.

    ``code`` must be an int between 100 and 599 and may not be 204 or 304 for a
    body-bearing type; both are checked when the app is built.
    ``description`` is the OpenAPI response description.
    """

    code: int
    description: str | None = None


#: Every typed-result class; the dispatcher unwraps instances of these.
RESULT_TYPES: tuple[type, ...] = (Ok, Created, Accepted, NoContent)
