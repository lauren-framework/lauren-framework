"""``ConfigModule`` and the factory's Phase 2b (typed configuration).

``ConfigModule.for_root`` / ``for_feature`` return a freshly generated
``@module`` class carrying an immutable :class:`ConfigSpec` under the
private ``__lauren_config_specs__`` attribute. ``LaurenFactory.create``
calls :func:`install_config` after it has registered every provider and
before it compiles the DI graph: every spec in the module graph is
loaded and validated, all problems are raised together as one
:class:`~lauren.exceptions.ConfigValidationError`, and the resulting
instances are registered as singleton values.

Specs with an :class:`~lauren.config.AsyncSource` finish loading at the
start of ``LaurenApp.startup()`` through :class:`PendingConfig`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .._di.custom import use_factory, use_value
from ..decorators import module
from ..exceptions import DecoratorUsageError, DuplicateBindingError, StartupError
from ..logging import Logger
from ._loader import (
    CONFIG_SPECS_ATTR,
    DEFAULT_PROFILES,
    ConfigSpec,
    LoadRun,
    Profile,
    SpecLoad,
    build_node,
    config_error,
    default_sources,
    exposed_section_types,
    make_spec,
    resolve_profile,
)


def _not_loaded(token: type) -> Callable[[], Any]:
    def factory() -> Any:
        raise StartupError(
            f"{token.__name__} has not been loaded yet: settings are loaded by LaurenFactory.create, "
            "and settings fed by an AsyncSource only once `await app.startup()` has run",
            detail={"reason": "settings_not_loaded", "settings": token.__name__},
        )

    return factory


def _generated_module(spec: ConfigSpec, tokens: Sequence[type]) -> type:
    providers: list[Any] = [use_factory(provide=token, factory=_not_loaded(token)) for token in tokens]
    namespace = {
        "__doc__": f"Generated ConfigModule for {spec.settings_cls.__name__}.",
        "__module__": __name__,
        CONFIG_SPECS_ATTR: (spec,),
    }
    generated = type(f"ConfigModule[{spec.settings_cls.__name__}]", (), namespace)
    return module(providers=providers, exports=list(tokens))(generated)


class ConfigModule:
    """Factory for configuration modules: typed settings validated inside ``LaurenFactory.create``.

    ``for_root`` declares the application settings (global by default) and
    the app-wide profile / ``environ`` options; ``for_feature`` declares a
    module-scoped section. Each call returns a new ``@module`` class to put
    in ``@module(imports=[...])``.
    """

    @classmethod
    def for_root(
        cls,
        settings_cls: type,
        *,
        env_prefix: str = "",
        sources: Sequence[Any] | None = None,
        section: str | None = None,
        profile: str | None = None,
        profiles: Sequence[str] = DEFAULT_PROFILES,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
        is_global: bool = True,
        expose_sections: bool = False,
    ) -> type:
        """Return a module that loads ``settings_cls`` and provides it (globally by default).

        ``sources=None`` means ``.env``, ``.env.{profile}`` (both optional),
        then the environment. ``profile`` falls back to ``LAUREN_PROFILE``
        and then ``"development"``; ``environ`` replaces ``os.environ``
        for every settings class in the app (tests). ``overrides`` pins
        values by dotted field path. ``expose_sections=True`` also provides
        each nested section class as its own token.
        """
        spec = make_spec(
            "ConfigModule.for_root",
            settings_cls,
            env_prefix=env_prefix,
            sources=sources,
            section=section,
            profile=profile,
            profiles=profiles,
            environ=environ,
            overrides=overrides,
            is_root=True,
            is_global=bool(is_global),
            expose_sections=expose_sections,
        )
        return _finish_spec(spec)

    @classmethod
    def for_feature(
        cls,
        settings_cls: type,
        *,
        env_prefix: str = "",
        sources: Sequence[Any] | None = None,
        section: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        expose_sections: bool = False,
    ) -> type:
        """Return a module-scoped settings module: importers (and re-exporters) can inject it.

        ``sources=None`` inherits the root ``ConfigModule``'s sources (or the
        default stack). ``section`` names the TOML / YAML table the class
        reads, e.g. ``section="database"`` for ``[database]``.
        """
        spec = make_spec(
            "ConfigModule.for_feature",
            settings_cls,
            env_prefix=env_prefix,
            sources=sources,
            section=section,
            overrides=overrides,
            expose_sections=expose_sections,
        )
        return _finish_spec(spec)


def _finish_spec(spec: ConfigSpec) -> type:
    if spec.is_global:
        return _generated_module(spec, ())
    tokens: list[type] = [spec.settings_cls]
    if spec.expose_sections:
        # The generated module must declare and export the section tokens
        # now, so the class hints are read here (no configuration is read).
        try:
            node = build_node(spec.settings_cls, env_prefix=spec.env_prefix)
        except StartupError as exc:
            raise DecoratorUsageError(
                f"expose_sections=True needs the section types of {spec.settings_cls.__name__} when the "
                f"module is declared: {exc.message}",
                detail={"helper": "ConfigModule", "reason": "unresolvable_sections"},
            ) from exc
        exposed = tuple(exposed_section_types(node))
        tokens.extend(exposed)
        spec = _replace_exposed(spec, exposed)
    return _generated_module(spec, tokens)


def _replace_exposed(spec: ConfigSpec, exposed: tuple[type, ...]) -> ConfigSpec:
    import dataclasses

    return dataclasses.replace(spec, exposed=exposed)


# ---------------------------------------------------------------------------
# Phase 2b
# ---------------------------------------------------------------------------


class PendingConfig:
    """Specs waiting on async sources; resolved by ``LaurenApp.startup()``."""

    def __init__(self, profile: str, loads: list[SpecLoad], logger: Logger) -> None:
        self.profile = profile
        self.loads = loads
        self._logger = logger

    async def resolve(self, container: Any) -> None:
        """Await the async sources, validate, and seed the singleton cache."""
        errors: list[dict[str, Any]] = []
        for load in self.loads:
            await load.fill_async()
            load.resolve()
            errors.extend(load.async_errors + load.errors)
        if errors:
            raise config_error(self.profile, errors)
        for load in self.loads:
            for token, value in load.token_values():
                container.set_singleton(token, value)
        self._logger.log(
            "async configuration loaded: " + ", ".join(load.settings_name for load in self.loads),
            context="Config",
            settings=[load.settings_name for load in self.loads],
        )


def install_config(graph: Any, container: Any, *, logger: Logger) -> PendingConfig | None:
    """Phase 2b of ``LaurenFactory.create``: load, validate and register every settings spec.

    Returns a :class:`PendingConfig` when some spec still waits on an
    async source, else ``None``. Raises one ``ConfigValidationError``
    listing every problem across every ``ConfigModule`` in the graph.
    """
    entries: list[tuple[ConfigSpec, type]] = []
    for mod_cls in graph.modules:
        for spec in mod_cls.__dict__.get(CONFIG_SPECS_ATTR, ()):
            entries.append((spec, mod_cls))
    if not entries:
        return None
    entries.sort(key=lambda entry: not entry[0].is_root)
    roots = [spec for spec, _ in entries if spec.is_root]
    _check_roots(roots)
    root = roots[0] if roots else None
    environ = root.environ if root is not None else None
    profile, problem = resolve_profile(
        root.profile if root is not None else None,
        root.profiles if root is not None else DEFAULT_PROFILES,
        environ,
        helper="ConfigModule.for_root",
    )
    if problem is not None:
        raise config_error(profile.name, [problem])
    run = LoadRun(profile.name, environ)
    inherited = root.sources if root is not None and root.sources is not None else default_sources()
    importers = _importers(graph)
    loads: list[SpecLoad] = []
    for spec, mod_cls in entries:
        sources = (
            spec.sources if spec.sources is not None else (default_sources() if spec.is_root else inherited)
        )
        module_name = None if spec.is_root else (", ".join(importers.get(mod_cls, [])) or mod_cls.__name__)
        load = SpecLoad(spec, run, sources=sources, module_name=module_name)
        load.resolve()
        loads.append(load)
    errors = run.problems + [entry for load in loads for entry in load.errors]
    if errors:
        raise config_error(profile.name, errors)
    _register(loads, container)
    if Profile not in {p.cls for p in container.all_providers()}:
        container.register_custom(use_value(provide=Profile, value=profile), owning_module=None)
    _log_summary(logger, profile.name, run, loads)
    pending = [load for load in loads if load.pending]
    return PendingConfig(profile.name, pending, logger) if pending else None


def _check_roots(roots: list[ConfigSpec]) -> None:
    first = roots[0] if roots else None
    for other in roots[1:]:
        assert first is not None
        if (other.profile, other.profiles, other.environ) != (first.profile, first.profiles, first.environ):
            raise StartupError(
                f"ConfigModule.for_root({first.settings_cls.__name__}) and "
                f"ConfigModule.for_root({other.settings_cls.__name__}) disagree on profile=, profiles= or "
                "environ=; these are app-wide, so pass the same values (or use for_feature)",
                detail={
                    "reason": "conflicting_config_roots",
                    "settings": [first.settings_cls.__name__, other.settings_cls.__name__],
                },
            )


def _importers(graph: Any) -> dict[type, list[str]]:
    found: dict[type, list[str]] = {}
    for mod_cls, compiled in graph.modules.items():
        for imported in compiled.meta.imports:
            if isinstance(imported, type) and CONFIG_SPECS_ATTR in imported.__dict__:
                found.setdefault(imported, []).append(mod_cls.__name__)
    return found


def _register(loads: list[SpecLoad], container: Any) -> None:
    existing = {p.cls for p in container.all_providers()}
    for load in loads:
        if not load.spec.is_global:
            if not load.pending:
                for token, value in load.token_values():
                    container.set_singleton(token, value)
            continue
        pairs = load.token_values() if not load.pending else [(t, None) for t in load.tokens()]
        for token, value in pairs:
            if token in existing:
                raise DuplicateBindingError(
                    f"{token.__name__} is provided by ConfigModule.for_root({load.settings_name}) and is also "
                    "registered elsewhere (another ConfigModule, a module provider or global_providers)",
                    detail={"token": token.__name__, "settings": load.settings_name},
                )
            existing.add(token)
            if load.pending:
                container.register_custom(
                    use_factory(provide=token, factory=_not_loaded(token)), owning_module=None
                )
            else:
                container.register_custom(use_value(provide=token, value=value), owning_module=None)


def _log_summary(logger: Logger, profile: str, run: LoadRun, loads: list[SpecLoad]) -> None:
    for load in loads:
        for node in load.node.nodes():
            params = getattr(node.cls, "__dataclass_params__", None)
            if node.kind == "dataclass" and params is not None and not params.frozen:
                logger.warn(
                    f"settings class {node.cls.__name__} is not frozen; declare it @dataclass(frozen=True) "
                    "so configuration cannot change at runtime",
                    context="Config",
                    settings=node.cls.__name__,
                )
    labels = run.read_labels()
    for load in loads:
        labels.extend(f"{layer.label} (async)" for layer in load.layers if layer.kind == "async")
    labels = list(dict.fromkeys(labels))
    summary = " ".join(f"{load.settings_name}({sum(1 for _ in load.node.leaves())} keys)" for load in loads)
    logger.log(
        f"profile={profile} settings={summary} sources={', '.join(labels) or '(none)'}",
        context="Config",
        profile=profile,
        settings={load.settings_name: sum(1 for _ in load.node.leaves()) for load in loads},
        sources=labels,
    )


__all__ = ["ConfigModule", "PendingConfig", "install_config"]
