"""Settings loader: bind layered sources to a settings class, aggregate every error.

The loader walks a settings class once (``resolve_type_hints``-based, so
PEP 563 annotations work), looks every leaf field up in the source
layers — later layers win, ``overrides=`` win over everything — coerces
raw strings with :func:`lauren._config._coerce.coerce`, and hands the
typed values to :func:`lauren._validation.validate_as` for the final
construction of each (nested) settings object. Every problem is
collected, with the key that was searched and the source that supplied
the value, and raised as one :class:`~lauren.exceptions.ConfigValidationError`.

Nothing here is cached at module level: a load is an object that lives
for one ``LaurenFactory.create`` (or :func:`load_settings`) call.
"""

from __future__ import annotations

import dataclasses
import os
import re
import types
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ForwardRef, TypeVar, Union, get_args, get_origin

from ..exceptions import ConfigValidationError, DecoratorUsageError, ExtractorError, StartupError
from .._validation import is_dataclass, is_msgspec_struct, is_pydantic_model, is_typeddict, validate_as
from ._coerce import CoercionError, UnsupportedTypeError, coerce
from ._secret import is_secret_type
from ._sources import (
    DotenvSource,
    EnvSource,
    SourceProblem,
    _FileSource,
    _Layer,
    _no_lookup,
    fill_async_layer,
    is_async_source,
    load_layer,
    source_label,
)

T = TypeVar("T")

#: Profiles accepted when ``profiles=`` is not given.
DEFAULT_PROFILES: tuple[str, ...] = ("development", "test", "production")
#: Environment variable that selects the active profile.
PROFILE_ENV = "LAUREN_PROFILE"
#: Private module-class attribute carrying the specs of a generated ConfigModule.
CONFIG_SPECS_ATTR = "__lauren_config_specs__"

_UNION_TYPES: tuple[Any, ...] = (Union, types.UnionType)


def default_sources() -> tuple[Any, ...]:
    """The stack used when ``sources=None``: ``.env``, ``.env.{profile}``, environment."""
    return (
        DotenvSource(".env", required=False),
        DotenvSource(".env.{profile}", required=False),
        EnvSource(),
    )


# ---------------------------------------------------------------------------
# Public value types: Env, Profile
# ---------------------------------------------------------------------------


class Env:
    """``Annotated`` metadata naming the environment variable(s) for a field.

    ``Annotated[str, Env("DATABASE_URL", "DB_URL")]`` reads the first name
    present, with no ``env_prefix`` applied.
    """

    __slots__ = ("names",)

    def __init__(self, *names: str) -> None:
        if not names:
            raise DecoratorUsageError(
                'Env() needs at least one environment variable name, e.g. Env("DATABASE_URL")',
                detail={"helper": "Env", "reason": "no_names"},
            )
        for name in names:
            if not isinstance(name, str) or not name:
                raise DecoratorUsageError(
                    f"Env() names must be non-empty strings, got {name!r}",
                    detail={"helper": "Env", "reason": "invalid_name"},
                )
        self.names: tuple[str, ...] = names

    def __repr__(self) -> str:
        return "Env(" + ", ".join(repr(n) for n in self.names) + ")"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Env) and other.names == self.names

    def __hash__(self) -> int:
        return hash((Env, self.names))


@dataclass(frozen=True, slots=True)
class Profile:
    """The active configuration profile (``development``, ``test``, ``production``, ...).

    Registered globally by every app that imports a ``ConfigModule``, so a
    provider can declare ``profile: Profile``.
    """

    name: str

    @property
    def is_development(self) -> bool:
        return self.name == "development"

    @property
    def is_test(self) -> bool:
        return self.name == "test"

    @property
    def is_production(self) -> bool:
        return self.name == "production"


# ---------------------------------------------------------------------------
# Specs (what a ConfigModule / load_settings call declares)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class ConfigSpec:
    """Immutable description of one settings registration."""

    settings_cls: type
    env_prefix: str = ""
    sources: tuple[Any, ...] | None = None
    section: tuple[str, ...] = ()
    profile: str | None = None
    profiles: tuple[str, ...] = DEFAULT_PROFILES
    environ: Mapping[str, str] | None = None
    overrides: Mapping[str, Any] | None = None
    is_root: bool = False
    is_global: bool = False
    expose_sections: bool = False
    #: Section tokens of a module-scoped spec, computed when the module is declared.
    exposed: tuple[type, ...] = ()


def _usage(helper: str, message: str, reason: str) -> DecoratorUsageError:
    return DecoratorUsageError(f"{helper}: {message}", detail={"helper": helper, "reason": reason})


def settings_kind(cls: Any) -> str | None:
    """Return the backend for a settings class, or ``None`` if unsupported."""
    if not isinstance(cls, type):
        return None
    if any(
        klass.__name__ == "BaseSettings" and klass.__module__.startswith("pydantic_settings")
        for klass in cls.__mro__
    ):
        return "pydantic_settings"
    if is_pydantic_model(cls):
        return "pydantic"
    if is_msgspec_struct(cls):
        return "msgspec"
    if is_dataclass(cls):
        return "dataclass"
    return None


def make_spec(
    helper: str,
    settings_cls: Any,
    *,
    env_prefix: Any = "",
    sources: Any = None,
    section: Any = None,
    profile: Any = None,
    profiles: Any = DEFAULT_PROFILES,
    environ: Any = None,
    overrides: Any = None,
    is_root: bool = False,
    is_global: bool = False,
    expose_sections: bool = False,
) -> ConfigSpec:
    """Validate helper arguments eagerly and freeze them into a :class:`ConfigSpec`."""
    kind = settings_kind(settings_cls)
    if kind is None:
        if is_typeddict(settings_cls):
            raise _usage(
                helper,
                f"{settings_cls.__name__} is a TypedDict, which cannot express defaults or immutability; "
                "declare a @dataclass(frozen=True) instead",
                "unsupported_settings_class",
            )
        raise _usage(
            helper,
            f"expected a settings class (dataclass, pydantic model or msgspec Struct), got {settings_cls!r}",
            "unsupported_settings_class",
        )
    if not isinstance(env_prefix, str):
        raise _usage(helper, f"env_prefix must be a string, got {env_prefix!r}", "invalid_env_prefix")
    checked_sources = _check_sources(helper, sources)
    if section is not None and (not isinstance(section, str) or not section.strip(".")):
        raise _usage(
            helper,
            f'section must be a dotted table name such as "database", got {section!r}',
            "invalid_section",
        )
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise _usage(helper, f"profile must be a non-empty string, got {profile!r}", "invalid_profile")
    if isinstance(profiles, str) or not isinstance(profiles, Iterable):
        raise _usage(helper, f"profiles must be a tuple of names, got {profiles!r}", "invalid_profiles")
    checked_profiles = tuple(profiles)
    if not checked_profiles or not all(isinstance(p, str) and p for p in checked_profiles):
        raise _usage(
            helper, f"profiles must be non-empty strings, got {checked_profiles!r}", "invalid_profiles"
        )
    if environ is not None and not isinstance(environ, Mapping):
        raise _usage(
            helper,
            f"environ must be a mapping of str to str, got {type(environ).__name__}",
            "invalid_environ",
        )
    if overrides is not None and (
        not isinstance(overrides, Mapping) or not all(isinstance(k, str) and k.strip(".") for k in overrides)
    ):
        raise _usage(
            helper,
            'overrides must map dotted field paths to values, e.g. {"database.url": "sqlite://"}',
            "invalid_overrides",
        )
    if kind == "pydantic_settings" and (env_prefix or checked_sources is not None or section is not None):
        raise _usage(
            helper,
            f"{settings_cls.__name__} is a pydantic-settings BaseSettings, which reads its own sources; "
            "configure env_prefix / env files in its model_config instead of env_prefix=, sources= or section=",
            "delegated_settings_options",
        )
    return ConfigSpec(
        settings_cls=settings_cls,
        env_prefix=env_prefix,
        sources=checked_sources,
        section=tuple(p for p in section.split(".") if p) if section else (),
        profile=profile,
        profiles=checked_profiles,
        environ=environ,
        overrides=dict(overrides) if overrides is not None else None,
        is_root=is_root,
        is_global=is_global,
        expose_sections=bool(expose_sections),
    )


def _check_sources(helper: str, sources: Any) -> tuple[Any, ...] | None:
    if sources is None:
        return None
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Iterable):
        raise _usage(helper, f"sources must be a list of sources, got {sources!r}", "invalid_sources")
    items = tuple(sources)
    for index, source in enumerate(items):
        if isinstance(source, type):
            raise _usage(
                helper,
                f"sources[{index}] is the class {source.__name__}; pass an instance such as {source.__name__}()",
                "invalid_sources",
            )
        if not (isinstance(source, (EnvSource, _FileSource)) or is_async_source(source)):
            raise _usage(
                helper,
                f"sources[{index}] ({source!r}) is not a configuration source; use EnvSource(), DotenvSource(path), "
                "TomlSource(path), YamlSource(path), SecretsDirSource(path) or an AsyncSource",
                "invalid_sources",
            )
        if isinstance(source, EnvSource) and index != len(items) - 1:
            raise _usage(
                helper,
                "EnvSource() must be the last source so the real environment always wins over files",
                "env_source_not_last",
            )
    return items


# ---------------------------------------------------------------------------
# Settings schema: one walk of the settings class, at startup
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class _Field:
    name: str
    #: Key passed to the constructor (a pydantic alias or msgspec rename).
    key: str
    path: tuple[str, ...]
    tp: Any
    env_names: tuple[str, ...]
    required: bool
    secret: bool
    section: _Node | None = None
    optional_section: bool = False


@dataclass(frozen=True, eq=False)
class _Node:
    cls: type
    kind: str
    path: tuple[str, ...]
    fields: tuple[_Field, ...]

    def leaves(self) -> Iterator[_Field]:
        for f in self.fields:
            if f.section is not None:
                yield from f.section.leaves()
            else:
                yield f

    def section_fields(self) -> Iterator[_Field]:
        for f in self.fields:
            if f.section is not None:
                yield f
                yield from f.section.section_fields()

    def nodes(self) -> Iterator[_Node]:
        yield self
        for f in self.fields:
            if f.section is not None:
                yield from f.section.nodes()


def build_node(
    cls: type, *, env_prefix: str, path: tuple[str, ...] = (), stack: tuple[type, ...] = ()
) -> _Node:
    """Walk ``cls`` (and its nested sections) into a :class:`_Node` tree."""
    kind = settings_kind(cls) or "dataclass"
    if kind == "pydantic_settings" and path:
        kind = "pydantic"
    fields: list[_Field] = []
    for name, key, required, annotation, extra in _iter_fields(cls, kind):
        if isinstance(annotation, (str, ForwardRef)):
            text = annotation if isinstance(annotation, str) else annotation.__forward_arg__
            raise StartupError(
                f"cannot resolve the annotation {text!r} of {cls.__name__}.{name}; define or import "
                "the type at module level so the settings class can be inspected",
                detail={
                    "reason": "unresolvable_settings_annotation",
                    "settings": cls.__name__,
                    "field": name,
                },
            )
        inner, metadata = _split_annotated(annotation)
        env = next((m for m in metadata + extra if isinstance(m, Env)), None)
        field_path = path + (name,)
        env_names = env.names if env is not None else ((env_prefix + "__".join(field_path)).upper(),)
        section_cls, optional = _section_of(inner)
        section = None
        if section_cls is not None:
            if section_cls in stack or section_cls is cls:
                raise StartupError(
                    f"settings class {section_cls.__name__} contains itself through {cls.__name__}.{name}",
                    detail={"reason": "recursive_settings", "settings": section_cls.__name__, "field": name},
                )
            section = build_node(section_cls, env_prefix=env_prefix, path=field_path, stack=stack + (cls,))
        fields.append(
            _Field(
                name=name,
                key=key,
                path=field_path,
                tp=inner,
                env_names=env_names,
                required=required,
                secret=_contains_secret(inner),
                section=section,
                optional_section=optional,
            )
        )
    return _Node(cls=cls, kind=kind, path=path, fields=tuple(fields))


def _class_hints(cls: type) -> dict[str, Any]:
    from .._di import _safe_class_hints

    return _safe_class_hints(cls, include_extras=True)


def _iter_fields(cls: type, kind: str) -> Iterator[tuple[str, str, bool, Any, tuple[Any, ...]]]:
    """Yield ``(name, constructor key, required, annotation, extra metadata)`` per field.

    Dataclass annotations are resolved here (PEP 563 strings); pydantic and
    msgspec already resolved theirs when the class was created.
    """
    if kind == "dataclass":
        hints = _class_hints(cls)
        for f in dataclasses.fields(cls):
            if f.init:
                required = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
                yield f.name, f.name, required, hints.get(f.name, f.type), ()
    elif kind == "msgspec":
        import msgspec

        try:
            struct_fields = msgspec.structs.fields(cls)
        except NameError as exc:
            raise StartupError(
                f"cannot resolve the annotations of {cls.__name__} ({exc}); define or import the types "
                "at module level so the settings class can be inspected",
                detail={"reason": "unresolvable_settings_annotation", "settings": cls.__name__},
            ) from None
        for sf in struct_fields:
            yield sf.name, sf.encode_name, sf.required, sf.type, ()
    else:
        for name, info in cls.model_fields.items():  # type: ignore[attr-defined]
            alias = info.alias if isinstance(info.alias, str) else None
            yield name, alias or name, info.is_required(), info.annotation, tuple(info.metadata)


def _split_annotated(annotation: Any) -> tuple[Any, tuple[Any, ...]]:
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        inner, metadata = _split_annotated(args[0])
        return inner, tuple(args[1:]) + metadata
    return annotation, ()


def _strip_optional(tp: Any) -> Any:
    if get_origin(tp) in _UNION_TYPES:
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def _section_of(tp: Any) -> tuple[type | None, bool]:
    inner = _strip_optional(tp)
    optional = inner is not tp
    inner = _split_annotated(inner)[0]
    if settings_kind(inner) is not None:
        return inner, optional
    return None, False


# ---------------------------------------------------------------------------
# One load: profile, cached layers, collected source problems
# ---------------------------------------------------------------------------


def resolve_profile(
    profile: str | None, profiles: tuple[str, ...], environ: Mapping[str, str] | None, *, helper: str
) -> tuple[Profile, dict[str, Any] | None]:
    """Pick the active profile; the second item is an ``unknown_profile`` entry or ``None``."""
    if profile is not None:
        name, origin, key = profile, f"{helper}(profile=...)", None
    else:
        env: Mapping[str, str] = environ if environ is not None else os.environ
        raw = (env.get(PROFILE_ENV) or "").strip()
        if raw:
            name, origin, key = raw, "environment", PROFILE_ENV
        else:
            name, origin, key = "development", "default", None
    if name not in profiles:
        entry: dict[str, Any] = {
            "reason": "unknown_profile",
            "profile": name,
            "allowed": list(profiles),
            "source": origin,
            "message": f"profile {name!r} is not one of {', '.join(profiles)}",
        }
        if key is not None:
            entry["key"] = key
        return Profile(name), entry
    return Profile(name), None


class LoadRun:
    """State shared by every spec loaded in one ``create()`` / ``load_settings()`` call."""

    def __init__(self, profile: str, environ: Mapping[str, str] | None) -> None:
        self.profile = profile
        self.environ = environ
        self.problems: list[dict[str, Any]] = []
        self._layers: dict[Any, _Layer | None] = {}

    def layer(self, source: Any) -> _Layer | None:
        """Return the layer for ``source`` (file layers are read once per run)."""
        if is_async_source(source):
            return load_layer(source, profile=self.profile, environ=self.environ)
        key = (type(source), source_label(source, self.profile))
        if key not in self._layers:
            try:
                self._layers[key] = load_layer(source, profile=self.profile, environ=self.environ)
            except SourceProblem as problem:
                self._layers[key] = None
                self.problems.append(problem.entry)
        return self._layers[key]

    def read_labels(self) -> list[str]:
        """Labels of the sources that actually provided data, in first-use order."""
        return [layer.label for layer in self._layers.values() if layer is not None and layer.read]


# ---------------------------------------------------------------------------
# Resolution of one spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Found:
    value: Any
    label: str
    key: str
    lax: bool


class _Marker:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


_FAILED: Any = _Marker("FAILED")
_DEFERRED: Any = _Marker("DEFERRED")
_ABSENT: Any = _Marker("ABSENT")

_MSGSPEC_PATH = re.compile(r"\.([^.\[]+)|\[(\d+)\]")


class SpecLoad:
    """Loads one :class:`ConfigSpec` against the layers of a :class:`LoadRun`."""

    def __init__(
        self,
        spec: ConfigSpec,
        run: LoadRun,
        *,
        sources: Sequence[Any],
        module_name: str | None = None,
    ) -> None:
        self.spec = spec
        self.run = run
        self.module_name = module_name
        self.settings_name = spec.settings_cls.__name__
        env_prefix = spec.env_prefix
        if settings_kind(spec.settings_cls) == "pydantic_settings":
            # Delegated: pydantic-settings reads its own sources.
            env_prefix = str(getattr(spec.settings_cls, "model_config", {}).get("env_prefix", ""))
            sources = ()
        self.node = build_node(spec.settings_cls, env_prefix=env_prefix)
        self.searched = [source_label(s, run.profile) for s in sources] or (
            ["pydantic-settings"] if self.node.kind == "pydantic_settings" else []
        )
        layers = [run.layer(s) for s in sources]
        self.layers: list[_Layer] = [layer for layer in layers if layer is not None]
        self.value: Any = _ABSENT
        self.errors: list[dict[str, Any]] = []
        self.async_errors: list[dict[str, Any]] = []
        self._provenance: dict[tuple[str, ...], _Found] = {}
        self._overrides, self._override_errors = self._override_tree()

    # -- public ---------------------------------------------------------

    @property
    def pending(self) -> bool:
        return any(layer.pending for layer in self.layers)

    @property
    def keys(self) -> list[str]:
        """Every environment-variable name the settings class can read."""
        return list(dict.fromkeys(name for leaf in self.node.leaves() for name in leaf.env_names))

    def resolve(self) -> None:
        """(Re)compute ``value`` and ``errors`` from the current layers."""
        self.errors = list(self._override_errors)
        self._provenance = {}
        if self.node.kind == "pydantic_settings":
            self.value = self._load_delegated()
            return
        value, _ = self._build(self.node)
        self.value = _FAILED if self.errors else value

    async def fill_async(self) -> None:
        """(Re)load every async layer; a failing source becomes an error entry."""
        self.async_errors = []
        for layer in self.layers:
            if layer.kind == "async":
                try:
                    await fill_async_layer(layer, self.keys)
                except SourceProblem as problem:
                    layer.lookup = _no_lookup
                    self.async_errors.append(problem.entry)

    def tokens(self) -> list[type]:
        """The DI tokens this spec provides: the settings class, then exposed sections."""
        if not self.spec.expose_sections:
            return [self.spec.settings_cls]
        return [self.spec.settings_cls, *exposed_section_types(self.node)]

    def token_values(self) -> list[tuple[type, Any]]:
        """``(token, instance)`` pairs once :attr:`value` is loaded."""
        pairs: list[tuple[type, Any]] = [(self.spec.settings_cls, self.value)]
        if self.spec.expose_sections:
            for f in self.node.section_fields():
                assert f.section is not None
                pairs.append((f.section.cls, _walk_attrs(self.value, f.path)))
        return pairs

    # -- overrides ------------------------------------------------------

    def _override_tree(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        tree: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []
        for dotted, value in (self.spec.overrides or {}).items():
            parts = [p for p in dotted.split(".") if p]
            target = tree
            for part in parts[:-1]:
                nxt = target.get(part)
                if not isinstance(nxt, dict):
                    nxt = target[part] = {}
                target = nxt
            target[parts[-1]] = _plain(value)
        self._check_override_paths(tree, self.node, (), errors)
        return tree, errors

    def _check_override_paths(
        self, tree: Mapping[str, Any], node: _Node, prefix: tuple[str, ...], errors: list[dict[str, Any]]
    ) -> None:
        by_name = {f.name: f for f in node.fields}
        for key, value in tree.items():
            f = by_name.get(key)
            if f is None:
                errors.append(
                    self._entry(
                        prefix + (key,),
                        "unknown_field",
                        source="overrides",
                        message=f"overrides names unknown field {'.'.join(prefix + (key,))!r}",
                    )
                )
            elif f.section is not None and isinstance(value, Mapping):
                self._check_override_paths(value, f.section, prefix + (key,), errors)

    # -- lookups --------------------------------------------------------

    def _find(self, f: _Field) -> Any:
        """Return the winning :class:`_Found`, ``None`` (absent) or ``_DEFERRED``."""
        result: _Found | None = None
        pending_after = False
        for layer in self.layers:
            if layer.pending:
                pending_after = True
                continue
            hit = self._lookup(layer, f)
            if hit is not None:
                result, pending_after = hit, False
        override = _walk_tree(self._overrides, f.path)
        if override is not _ABSENT:
            return _Found(override, "overrides", ".".join(f.path), lax=isinstance(override, str))
        if pending_after:
            return _DEFERRED
        return result

    def _lookup(self, layer: _Layer, f: _Field) -> _Found | None:
        if layer.kind == "nested":
            value = _walk_tree(layer.tree or {}, self.spec.section + f.path)
            if value is _ABSENT:
                return None
            return _Found(value, layer.label, ".".join(self.spec.section + f.path), lax=False)
        assert layer.lookup is not None
        for name in f.env_names:
            raw = layer.lookup(name)
            if raw is not None:
                return _Found(raw, layer.label, name, lax=True)
        return None

    def _find_section(self, f: _Field) -> _Found | None:
        """A value supplied for a whole section (an override instance, or a scalar in a file)."""
        found: _Found | None = None
        for layer in self.layers:
            if layer.kind == "nested":
                value = _walk_tree(layer.tree or {}, self.spec.section + f.path)
                if value is not _ABSENT and not isinstance(value, Mapping):
                    found = _Found(value, layer.label, ".".join(self.spec.section + f.path), lax=False)
        override = _walk_tree(self._overrides, f.path)
        if override is not _ABSENT and not isinstance(override, Mapping):
            found = _Found(override, "overrides", ".".join(f.path), lax=False)
        return found

    # -- building -------------------------------------------------------

    def _build(self, node: _Node) -> tuple[Any, bool]:
        kwargs: dict[str, Any] = {}
        supplied = failed = deferred = coercion_failed = False
        for f in node.fields:
            if f.section is not None:
                direct = self._find_section(f)
                if direct is not None:
                    supplied = True
                    if (direct.value is None and f.optional_section) or isinstance(
                        direct.value, f.section.cls
                    ):
                        kwargs[f.key] = direct.value
                    else:
                        failed = True
                        self._invalid(f, direct, f.section.cls.__name__, "expected a table")
                    continue
                mark = len(self.errors)
                value, sub_supplied = self._build(f.section)
                if not sub_supplied and not f.required:
                    del self.errors[mark:]
                    continue
                supplied = supplied or sub_supplied
                if value is _FAILED:
                    failed = True
                elif value is _DEFERRED:
                    deferred = True
                else:
                    kwargs[f.key] = value
                continue
            found = self._find(f)
            if found is _DEFERRED:
                deferred = True
                continue
            if found is None:
                if f.required:
                    failed = True
                    self.errors.append(self._missing(f))
                continue
            supplied = True
            self._provenance[f.path] = found
            try:
                kwargs[f.key] = coerce(found.value, f.tp, strict=not found.lax)
            except UnsupportedTypeError as exc:
                if node.kind in ("pydantic", "msgspec"):
                    kwargs[f.key] = found.value  # the model's own validator handles the type
                else:
                    failed = coercion_failed = True
                    self._invalid(f, found, exc.expected, exc.reason)
            except CoercionError as exc:
                failed = coercion_failed = True
                self._invalid(f, found, exc.expected, exc.reason)
        if failed:
            if node.kind == "pydantic" and not coercion_failed and not deferred:
                # pydantic reports every error at once: surface the other
                # fields' constraint errors in the same pass.
                self._construct(node, kwargs, skip_missing=True)
            return _FAILED, supplied
        if deferred:
            return _DEFERRED, supplied
        return self._construct(node, kwargs), supplied

    def _construct(self, node: _Node, kwargs: dict[str, Any], *, skip_missing: bool = False) -> Any:
        label = ".".join((self.settings_name, *node.path))
        try:
            return validate_as(node.cls, kwargs, field=label, lax=True)
        except ExtractorError as exc:
            self._model_errors(node, exc.__context__ or exc, skip_missing=skip_missing)
            return _FAILED

    def _load_delegated(self) -> Any:
        try:
            return self.spec.settings_cls(**self._overrides)
        except Exception as exc:
            if not callable(getattr(exc, "errors", None)):
                raise
            self._model_errors(self.node, exc)
            return _FAILED

    # -- error mapping --------------------------------------------------

    def _model_errors(self, node: _Node, cause: BaseException, *, skip_missing: bool = False) -> None:
        errors_fn = getattr(cause, "errors", None)
        if callable(errors_fn):  # pydantic ValidationError
            for err in errors_fn():
                kind = err.get("type", "invalid")
                if not (skip_missing and kind == "missing"):
                    self._model_error(node, tuple(err.get("loc", ())), kind, err.get("msg", ""))
            return
        text = str(cause)
        if type(cause).__name__ == "ValidationError" and type(cause).__module__.startswith("msgspec"):
            message, _, where = text.partition(" - at `$")
            loc: list[Any] = [a or int(b) for a, b in _MSGSPEC_PATH.findall(where.rstrip("`"))]
            missing = re.match(r"Object missing required field `([^`]+)`", message)
            if missing:
                loc.append(missing.group(1))
            self._model_error(node, tuple(loc), "missing" if missing else "invalid", message)
            return
        self._model_error(node, (), "invalid", text)

    def _model_error(self, node: _Node, loc: tuple[Any, ...], kind: str, message: str) -> None:
        path, leaf = list(node.path), None
        current: _Node | None = node
        for item in loc:
            match = None
            if current is not None:
                match = next((f for f in current.fields if item in (f.key, f.name)), None)
            if match is None:
                path.append(str(item))
                current = None
                continue
            path, leaf, current = list(match.path), match, match.section
        if kind == "missing" and leaf is not None:
            self.errors.append(self._missing(leaf))
            return
        extra: dict[str, Any] = {"expected": kind, "message": message}
        if leaf is not None:
            extra["key"] = leaf.env_names[0]
            found = self._provenance.get(leaf.path)
            if found is not None:
                extra["key"] = found.key
                extra["source"] = found.label
                if not leaf.secret:
                    extra["value"] = _json_safe(found.value)
                    extra["message"] = f"{message}, got {found.value!r}"
        self.errors.append(self._entry(tuple(path), "invalid", **extra))

    def _entry(self, path: tuple[str, ...], reason: str, **extra: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {"settings": self.settings_name, "field": ".".join(path), "reason": reason}
        entry.update(extra)
        if self.module_name:
            entry["module"] = self.module_name
        return entry

    def _missing(self, f: _Field) -> dict[str, Any]:
        return self._entry(
            f.path,
            "missing",
            key=f.env_names[0],
            searched=list(self.searched) or ["(no sources)"],
            message="missing",
        )

    def _invalid(self, f: _Field, found: _Found, expected: str, reason: str) -> None:
        extra: dict[str, Any] = {"key": found.key, "expected": expected, "source": found.label}
        message = f"invalid {expected}"
        if not f.secret:
            extra["value"] = _json_safe(found.value)
            message += f" {found.value!r}"
        extra["message"] = message + (f": {reason}" if reason else "")
        self.errors.append(self._entry(f.path, "invalid", **extra))


def exposed_section_types(node: _Node) -> list[type]:
    """Section classes of ``node`` (recursively); a type used twice is rejected."""
    seen: dict[type, str] = {}
    for f in node.section_fields():
        assert f.section is not None
        dotted = ".".join(f.path)
        if f.section.cls in seen:
            from ..exceptions import DuplicateBindingError

            raise DuplicateBindingError(
                f"expose_sections=True cannot register {f.section.cls.__name__} twice "
                f"({node.cls.__name__}.{seen[f.section.cls]} and {node.cls.__name__}.{dotted}); "
                "inject the parent settings class instead",
                detail={"settings": node.cls.__name__, "section": f.section.cls.__name__},
            )
        seen[f.section.cls] = dotted
    return list(seen)


def _plain(value: Any) -> Any:
    """Copy nested mappings into fresh dicts so merging overrides never mutates the caller's."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _contains_secret(tp: Any) -> bool:
    """True when ``tp`` is a secret type or wraps one (``list[Secret[str]]``, ``Secret[int] | None``)."""
    return is_secret_type(tp) or any(_contains_secret(arg) for arg in get_args(tp))


def _walk_tree(tree: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = tree
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return _ABSENT
        current = current[part]
    return current


def _walk_attrs(value: Any, path: Sequence[str]) -> Any:
    for part in path:
        if value is None:
            return None
        value = getattr(value, part)
    return value


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


# ---------------------------------------------------------------------------
# Aggregated error
# ---------------------------------------------------------------------------


def format_entry(entry: dict[str, Any]) -> str:
    """Render one ``detail["errors"]`` entry as a line of the error message."""
    reason = entry["reason"]
    if reason in ("parse_error", "missing_file", "source_error"):
        return f"{entry['source']}: {entry['message']}"
    if reason == "unknown_profile":
        return f"{entry['message']} (from {entry['source']})"
    head = f"{entry['settings']}.{entry['field']}" if entry.get("field") else entry["settings"]
    if entry.get("key"):
        head += f"  [{entry['key']}]"
    if reason == "missing":
        line = f"{head}  missing (searched: {', '.join(entry['searched'])})"
    elif entry.get("source"):
        line = f"{head}  {entry['message']} (from {entry['source']})"
    else:
        line = f"{head}  {entry['message']}"
    if entry.get("module"):
        line += f"  (declared by {entry['module']})"
    return line


def config_error(profile: str, errors: list[dict[str, Any]]) -> ConfigValidationError:
    """Build the one :class:`ConfigValidationError` that carries every entry."""
    lines = "\n".join(f"  - {format_entry(e)}" for e in errors)
    return ConfigValidationError(
        f"{len(errors)} configuration error(s) in profile {profile!r}:\n{lines}",
        detail={"profile": profile, "errors": errors},
    )


# ---------------------------------------------------------------------------
# load_settings / aload_settings
# ---------------------------------------------------------------------------


def _prepare(helper: str, settings_cls: Any, kwargs: dict[str, Any]) -> tuple[SpecLoad, str]:
    spec = make_spec(helper, settings_cls, **kwargs)
    profile, problem = resolve_profile(spec.profile, spec.profiles, spec.environ, helper=helper)
    if problem is not None:
        raise config_error(profile.name, [problem])
    run = LoadRun(profile.name, spec.environ)
    load = SpecLoad(spec, run, sources=spec.sources if spec.sources is not None else default_sources())
    return load, profile.name


def _finish(load: SpecLoad, profile: str) -> Any:
    load.resolve()
    errors = load.run.problems + load.async_errors + load.errors
    if errors:
        raise config_error(profile, errors)
    return load.value


def load_settings(
    settings_cls: type[T],
    *,
    env_prefix: str = "",
    sources: Sequence[Any] | None = None,
    section: str | None = None,
    profile: str | None = None,
    profiles: Sequence[str] = DEFAULT_PROFILES,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> T:
    """Load and validate ``settings_cls`` outside the module graph.

    Uses the same sources, coercion and aggregated
    :class:`~lauren.exceptions.ConfigValidationError` as
    ``ConfigModule.for_root``. Meant for values needed *before*
    ``LaurenFactory.create`` (``sessions=``, ``docs_url=``, ``logger=``).
    """
    kwargs = dict(
        env_prefix=env_prefix, sources=sources, section=section, profile=profile,
        profiles=profiles, environ=environ, overrides=overrides,
    )  # fmt: skip
    if sources is not None and any(is_async_source(s) for s in sources):
        raise _usage(
            "load_settings",
            "an AsyncSource must be awaited; call `await aload_settings(...)` instead",
            "async_source_in_sync_loader",
        )
    load, active = _prepare("load_settings", settings_cls, kwargs)
    return _finish(load, active)  # type: ignore[no-any-return]


async def aload_settings(
    settings_cls: type[T],
    *,
    env_prefix: str = "",
    sources: Sequence[Any] | None = None,
    section: str | None = None,
    profile: str | None = None,
    profiles: Sequence[str] = DEFAULT_PROFILES,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> T:
    """Async twin of :func:`load_settings` that also awaits :class:`AsyncSource` sources."""
    kwargs = dict(
        env_prefix=env_prefix, sources=sources, section=section, profile=profile,
        profiles=profiles, environ=environ, overrides=overrides,
    )  # fmt: skip
    load, active = _prepare("aload_settings", settings_cls, kwargs)
    await load.fill_async()
    return _finish(load, active)  # type: ignore[no-any-return]


__all__ = [
    "CONFIG_SPECS_ATTR",
    "DEFAULT_PROFILES",
    "PROFILE_ENV",
    "ConfigSpec",
    "Env",
    "LoadRun",
    "Profile",
    "SpecLoad",
    "aload_settings",
    "build_node",
    "config_error",
    "default_sources",
    "exposed_section_types",
    "format_entry",
    "load_settings",
    "make_spec",
    "resolve_profile",
    "settings_kind",
]
