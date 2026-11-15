"""Deterministic, lifecycle-free manifest of a compiled Lauren application.

Read the graph assembled by ``LaurenFactory.create`` without resolving providers
or starting lifecycle hooks. Render names and metadata into JSON-safe snapshots;
never retain live application objects in the manifest.
"""

from __future__ import annotations

import hashlib
import json
import types
from dataclasses import asdict, dataclass, field
from typing import Any, ForwardRef, Literal, Mapping, Union, get_args, get_origin

from .._config._secret import Secret
from .._di.custom import Token


@dataclass(frozen=True, slots=True)
class ManifestDependency:
    """One compiled provider dependency."""

    name: str
    token: str


@dataclass(frozen=True, slots=True)
class ManifestProvider:
    """A provider binding, including its scope and lifecycle hooks."""

    token: str
    kind: str
    scope: str
    module: str | None
    provides: tuple[str, ...]
    multi: bool
    deps: tuple[ManifestDependency, ...]
    lifecycle: dict[str, tuple[str, ...]]
    generator: bool


@dataclass(frozen=True, slots=True)
class ManifestModule:
    """One module's own declarations, imports and exports."""

    name: str
    imports: tuple[str, ...]
    exports: tuple[str, ...]
    providers: tuple[str, ...]
    controllers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManifestParameter:
    """One route's compiled extractor parameter."""

    name: str
    source: str
    type: str
    required: bool


@dataclass(frozen=True, slots=True)
class ManifestRoute:
    """One compiled HTTP route and its effective policy chain."""

    method: str
    path: str
    name: str | None
    handler: str
    module: str | None
    binding: str
    is_coroutine: bool
    params: tuple[ManifestParameter, ...]
    guards: tuple[str, ...]
    interceptors: tuple[str, ...]
    middlewares: tuple[str, ...]
    exception_handlers: tuple[str, ...]
    metadata_keys: tuple[str, ...]
    encoder: str | None
    streaming_item: str | None
    status_code: int | None
    version: str | None
    include_in_schema: bool
    deprecated: bool
    tags: tuple[str, ...]
    operation_id: str | None
    metadata_values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ManifestEvent:
    """One WebSocket event and its compiled payload type."""

    event: str
    payload: str | None
    handler: str
    guards: tuple[str, ...] = ()
    interceptors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ManifestGateway:
    """One compiled WebSocket gateway."""

    path: str
    cls: str
    module: str | None
    events: tuple[ManifestEvent, ...]
    guards: tuple[str, ...]
    interceptors: tuple[str, ...]
    middlewares: tuple[str, ...]
    metadata_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManifestMount:
    """A mounted sub-application and its path prefix."""

    prefix: str
    app: str


@dataclass(frozen=True, slots=True)
class ManifestApp:
    """App-level configuration relevant to diagnostics and contract snapshots."""

    root_module: str
    error_format: str
    json_encoder: str
    max_body_size: int
    root_path: str
    docs: dict[str, str | None]
    sessions: dict[str, Any]
    profile: str | None = None
    debug: bool = False


@dataclass(frozen=True, slots=True)
class ManifestGlobals:
    """The app-level policy chains and globally registered providers."""

    middlewares: tuple[str, ...]
    guards: tuple[str, ...]
    interceptors: tuple[str, ...]
    exception_handlers: tuple[str, ...]
    ws_guards: tuple[str, ...]
    ws_interceptors: tuple[str, ...]
    providers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManifestOpenAPI:
    """The OpenAPI document's content hash and operation count."""

    sha256: str
    operations: int


@dataclass(frozen=True, slots=True)
class AppManifest:
    """A versioned, deterministic snapshot of an app's compiled graph.

    Call :meth:`to_json` to save or diff the manifest; no lifecycle methods or
    network connections run while building it. The schema is additive within
    one major manifest version.
    """

    manifest_version: int
    lauren_version: str
    app: ManifestApp
    globals: ManifestGlobals
    modules: tuple[ManifestModule, ...]
    providers: tuple[ManifestProvider, ...]
    routes: tuple[ManifestRoute, ...]
    websocket_gateways: tuple[ManifestGateway, ...]
    mounts: tuple[ManifestMount, ...]
    openapi: ManifestOpenAPI
    sections: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe copy of the manifest."""
        return asdict(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize the manifest with deterministic key ordering."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, ensure_ascii=False)


def _name(value: Any) -> str:
    """Render a token, callable, class or annotation without object addresses."""
    if isinstance(value, Token):
        return f"Token({value.name})"
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, ForwardRef):
        return value.__forward_arg__
    if value is None or value is type(None):
        return "None"
    if value is Any:
        return "Any"
    origin = get_origin(value)
    if origin is Union or origin is types.UnionType:
        return " | ".join(_name(item) for item in get_args(value))
    if origin is Literal:
        return f"Literal[{', '.join(repr(item) for item in get_args(value))}]"
    if origin is not None:
        args = get_args(value)
        base = _name(origin)
        return f"{base}[{', '.join(_name(a) for a in args)}]" if args else base
    module = getattr(value, "__module__", None)
    qualname = getattr(value, "__qualname__", None)
    if isinstance(module, str) and isinstance(qualname, str):
        return qualname if module == "builtins" else f"{module}.{qualname}"
    return _name(type(value))


def _metadata_key(key: Any) -> str:
    """Name a metadata key without revealing the value (or an object repr)."""
    if isinstance(key, str):
        return key
    name = getattr(key, "name", None)
    return name if isinstance(name, str) else _name(key)


def _safe_value(value: Any) -> Any:
    """Render explicit metadata values without disclosing wrapped secrets."""
    if isinstance(value, Secret):
        return "***"
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        return {
            _metadata_key(k): _safe_value(v)
            for k, v in sorted(value.items(), key=lambda kv: _metadata_key(kv[0]))
        }
    if isinstance(value, tuple | list):
        return [_safe_value(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_safe_value(item) for item in value), key=str)
    return _name(value)


def _session_details(app: Any) -> dict[str, Any]:
    """Describe an installed session engine without showing signing material."""
    from .._sessions._engine import _SessionEngine

    engine = app.container.singletons().get(_SessionEngine)
    if engine is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "cookie_name": engine.cookie_name,
        "store": _name(type(engine.store)),
        "secure": engine.secure,
    }


def _openapi_details(app: Any) -> ManifestOpenAPI:
    """Hash the generated document without running the application."""
    spec = app.openapi()
    data = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    operations = sum(
        method.upper() in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"}
        for path in spec.get("paths", {}).values()
        for method in path
    )
    return ManifestOpenAPI(hashlib.sha256(data).hexdigest(), operations)


def describe_app(app: Any, *, include_metadata_values: bool = False) -> AppManifest:
    """Describe the compiled app before or after its lifecycle starts.

    Read the factory's immutable graph. Never resolve providers, invoke
    ``@post_construct`` hooks or expose user metadata values by default;
    ``include_metadata_values=True`` retains only JSON-safe snapshots and
    redacts :class:`~lauren.config.Secret` values even when nested.
    """
    from .. import __version__
    from .._config._loader import Profile

    profile = app.container.singletons().get(Profile)
    graph = app.module_graph
    globals_ = ManifestGlobals(
        middlewares=tuple(map(_name, app._global_middlewares)),
        guards=tuple(map(_name, app._global_guards)),
        interceptors=tuple(map(_name, app._global_interceptors)),
        exception_handlers=tuple(map(_name, app._global_exception_handlers)),
        ws_guards=tuple(map(_name, app._global_ws_guards)),
        ws_interceptors=tuple(map(_name, app._global_ws_interceptors)),
        providers=tuple(sorted(_name(getattr(p, "provide", p)) for p in app._global_providers)),
    )
    modules = tuple(
        sorted(
            (
                ManifestModule(
                    name=_name(cls),
                    imports=tuple(sorted(map(_name, mod.imports))),
                    exports=tuple(sorted(map(_name, mod.exported))),
                    providers=tuple(sorted(map(_name, mod.own_providers))),
                    controllers=tuple(sorted(map(_name, mod.controllers))),
                )
                for cls, mod in graph.modules.items()
            ),
            key=lambda mod: mod.name,
        )
    )
    providers = tuple(
        sorted(
            (
                ManifestProvider(
                    token=_name(provider.cls),
                    kind=provider.provider_kind or ("function" if provider.is_function_provider else "class"),
                    scope=provider.scope.name.lower(),
                    module=_name(provider.owning_module) if provider.owning_module else None,
                    provides=tuple(sorted(map(_name, provider.provides))),
                    multi=provider.multi,
                    deps=tuple(
                        ManifestDependency(name, _name(token))
                        for name, token in (
                            *provider.deps,
                            *provider.field_deps,
                            *(
                                (("<alias>", provider.alias_target),)
                                if provider.provider_kind == "existing"
                                else ()
                            ),
                        )
                    ),
                    lifecycle={
                        "post_construct": tuple(hook.__name__ for hook in provider.post_construct_chain),
                        "pre_destruct": tuple(hook.__name__ for hook in provider.pre_destruct_chain),
                    },
                    generator=provider.is_generator_provider,
                )
                for provider in app.container.registered_providers()
            ),
            key=lambda provider: (provider.token, provider.module or "", provider.kind),
        )
    )
    routes = tuple(
        ManifestRoute(
            method=method,
            path=path,
            name=getattr(ch.route_meta, "name", None),
            handler=_name(ch.handler_fn),
            module=_name(ch.owning_module) if ch.owning_module else None,
            binding=ch.binding,
            is_coroutine=ch.is_coroutine,
            params=tuple(
                ManifestParameter(ext.name, ext.source, _name(ext.inner_type), not ext.has_default)
                for ext in ch.extractions
            ),
            guards=tuple(map(_name, (*app._global_guards, *ch.guards))),
            interceptors=tuple(map(_name, (*app._global_interceptors, *ch.interceptors))),
            middlewares=tuple(map(_name, (*app._global_middlewares, *ch.middleware_chain))),
            exception_handlers=tuple(map(_name, (*ch.exception_handlers, *app._global_exception_handlers))),
            metadata_keys=tuple(sorted(map(_metadata_key, ch.metadata))),
            encoder=_name(type(ch.encoder)) if ch.encoder is not None else None,
            streaming_item=_name(ch.streaming_item_type) if ch.streaming_item_type is not None else None,
            status_code=ch.response_table.default_status
            if ch.response_table is not None
            else ch.route_meta.status_code,
            version=getattr(ch.route_meta, "version", None),
            include_in_schema=ch.route_meta.include_in_schema,
            deprecated=ch.route_meta.deprecated,
            tags=tuple(ch.route_meta.tags),
            operation_id=ch.route_meta.operation_id,
            metadata_values={
                _metadata_key(k): _safe_value(v)
                for k, v in sorted(ch.metadata.items(), key=lambda kv: _metadata_key(kv[0]))
                if include_metadata_values
                or any(key.name == k and key.public for key in ch.metadata_key_defs)
            },
        )
        for (method, path), ch in sorted(app._handlers.items(), key=lambda item: (item[0][1], item[0][0]))
    )
    gateways = tuple(
        ManifestGateway(
            path=path,
            cls=_name(gw.controller_cls),
            module=_name(gw.owning_module) if gw.owning_module else None,
            events=tuple(
                ManifestEvent(
                    event=event,
                    payload=(
                        _name(next((ext.inner_type for ext in msg.extractions if ext.source == "json"), Any))
                        if msg.payload_param is not None
                        else None
                    ),
                    handler=_name(msg.handler_fn),
                    guards=tuple(map(_name, msg.guards)),
                    interceptors=tuple(map(_name, msg.interceptors)),
                )
                for event, msg in sorted(gw.messages.items())
            ),
            guards=tuple(map(_name, (*app._global_ws_guards, *gw.guards, *gw.on_connect_guards))),
            interceptors=tuple(map(_name, (*app._global_ws_interceptors, *gw.interceptors))),
            middlewares=tuple(map(_name, gw.middlewares)),
            metadata_keys=tuple(sorted(map(_metadata_key, gw.metadata))),
        )
        for path, gw in sorted(app._ws_gateways.items())
    )
    mounts = tuple(
        ManifestMount(prefix, _name(type(subapp)))
        for prefix, subapp in sorted(app.mounts, key=lambda mount: mount[0])
    )
    app_config = ManifestApp(
        root_module=_name(graph.root),
        error_format=app.error_format,
        json_encoder=_name(type(app._json_encoder)),
        max_body_size=app._max_body_size,
        root_path=app._root_path,
        docs={
            "openapi_url": app._openapi_url,
            "docs_url": app._docs_url,
            "redoc_url": app._redoc_url,
        },
        sessions=_session_details(app),
        profile=profile.name if profile is not None else None,
        debug=app._debug is not None,
    )
    return AppManifest(
        manifest_version=1,
        lauren_version=__version__,
        app=app_config,
        globals=globals_,
        modules=modules,
        providers=providers,
        routes=routes,
        websocket_gateways=gateways,
        mounts=mounts,
        openapi=_openapi_details(app),
    )


__all__ = [
    "AppManifest",
    "ManifestApp",
    "ManifestDependency",
    "ManifestEvent",
    "ManifestGateway",
    "ManifestGlobals",
    "ManifestModule",
    "ManifestMount",
    "ManifestOpenAPI",
    "ManifestParameter",
    "ManifestProvider",
    "ManifestRoute",
    "describe_app",
]
