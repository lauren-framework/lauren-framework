# Decorators

All user-facing class and method decorators exported by the framework.

## Module system

::: lauren.module

::: lauren.controller

::: lauren.injectable

## HTTP route decorators

Every verb decorator takes the same options, validated at decoration time
(`DecoratorUsageError` with `detail={"decorator", "target", "option", "reason"}`):
`status_code`, `response_validation`, `response_headers` (see
[Response Contracts](../guides/response-contracts.md)) and `consumes`,
`produces`, `max_body_size` (see
[Content Negotiation & Body Limits](../guides/content-negotiation.md)).
`@controller(..., consumes=..., produces=..., max_body_size=...)` sets the
defaults of its routes.

::: lauren.get

::: lauren.post

::: lauren.put

::: lauren.patch

::: lauren.delete

::: lauren.head

::: lauren.options

## Middleware & Guards

::: lauren.middleware

::: lauren.use_middlewares

::: lauren.use_guards

::: lauren.interceptor

::: lauren.use_interceptors

::: lauren.exception_handler

::: lauren.use_exception_handlers

## Metadata, state keys & composition

See the [Typed Metadata, State Keys & Composition guide](../guides/typed-metadata-and-state.md).

::: lauren.set_metadata

::: lauren.MetadataKey

::: lauren.MetadataSetKey

::: lauren.StateKey

::: lauren.provides_state

::: lauren.compose

::: lauren.reject_bare_usage

::: lauren.propagate_metadata

## Lifecycle

::: lauren.post_construct

::: lauren.pre_destruct

## Scope

::: lauren.Scope
