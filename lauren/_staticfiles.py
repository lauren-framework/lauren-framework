"""Static file serving for lauren applications — NestJS-inspired module API.

:class:`StaticFilesModule` mirrors NestJS's ``ServeStaticModule``: a call to
:meth:`StaticFilesModule.for_root` returns a fully configured ``@module``
class that can be imported like any other feature module.

Usage::

    from lauren import LaurenFactory, module
    from lauren.static_files import StaticFilesModule

    @module(
        imports=[
            StaticFilesModule.for_root("/static", directory="./public"),
        ],
        controllers=[...],
    )
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

Requests whose path starts with the configured prefix are handled by a
generated controller and served directly from the filesystem.  All other
requests pass through to the normal router.

Security
--------
Path traversal is blocked: the resolved file path must remain inside the
configured directory.  Paths that escape (``../../`` tricks) receive a 403.
Missing files return a plain 404 with an empty body.

Dotfiles are not served by default: a request whose path has *any* segment
starting with ``.`` (``/.env``, ``/.git/config``, ``/a/.hidden/b.txt``) gets
the same empty 404 as a missing file, so existence is not revealed. The
policy is checked on the request path and again on the resolved path, so
``..`` detours and symlinks into dot-directories are covered.  The
``dotfiles=`` option of :meth:`StaticFilesModule.for_root` selects the policy:
``"allow_well_known"`` (default) still serves ``/.well-known/**`` (RFC 8615),
``"deny"`` blocks it too, and ``"allow"`` serves every dotfile.

Caching
-------
An ``ETag`` derived from the MD5 hash of the file contents is set on every
200 response.  Clients that echo it back in ``If-None-Match`` receive a 304
Not Modified without re-reading the body.  A ``Cache-Control: public,
max-age=<max_age>`` header (default one hour) is also attached.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import posixpath
from pathlib import Path as _FSPath
from typing import Literal

from .decorators import controller, get, module
from .extractors import Path as PathParam
from .types import Request, Response


DotfilesPolicy = Literal["deny", "allow_well_known", "allow"]

_DOTFILES_POLICIES: frozenset[str] = frozenset({"deny", "allow_well_known", "allow"})


def _is_hidden(relative: str, *, allow_well_known: bool) -> bool:
    """Return whether *relative* names a dotfile path the policy must hide.

    Applied twice: to the normalized URL path before the filesystem is
    touched, and to the resolved path relative to the served root, which is
    what actually decides. A request path that escapes the root (``../x``)
    passes the first check and is judged after resolution — it either lands
    outside the root (403) or back inside it, where its real segments count.
    """
    segments = posixpath.normpath(relative.replace("\\", "/")).split("/")
    if segments[0] == "..":
        return False
    if allow_well_known and segments[0] == ".well-known":
        segments = segments[1:]
    return any(segment.startswith(".") for segment in segments)


def _serve_file(
    base_dir: _FSPath,
    relative: str,
    request: Request,
    max_age: int,
    *,
    dotfiles: DotfilesPolicy = "allow_well_known",
) -> Response:
    """Resolve *relative* inside *base_dir* and return the appropriate response.

    Returns 200 with the file body, 304 when the ETag matches
    ``If-None-Match``, 403 on path traversal, or 404 for missing files and
    for dotfile paths the *dotfiles* policy hides.
    """
    if not relative or relative in ("/", ""):
        relative = "index.html"
    relative = relative.lstrip("/")

    if dotfiles != "allow" and _is_hidden(relative, allow_well_known=dotfiles == "allow_well_known"):
        return Response(b"", status=404)

    try:
        file_path = (base_dir / relative).resolve()
    except (ValueError, OSError):
        return Response(b"", status=404)

    try:
        resolved = file_path.relative_to(base_dir)
    except ValueError:
        return Response(b"", status=403)

    # Judge the path that will actually be read: ``/../public/.env`` or a
    # symlink into a dot-directory must not get past the policy.
    if dotfiles != "allow" and _is_hidden(
        resolved.as_posix(), allow_well_known=dotfiles == "allow_well_known"
    ):
        return Response(b"", status=404)

    if not file_path.is_file():
        return Response(b"", status=404)

    data = file_path.read_bytes()
    etag = '"' + hashlib.md5(data).hexdigest() + '"'  # noqa: S324

    inm = request.headers.get("if-none-match", "")
    if inm and etag in {e.strip() for e in inm.split(",")}:
        return Response(b"", status=304).with_header("etag", etag)

    content_type, _ = mimetypes.guess_type(str(file_path))
    resp = Response(
        data,
        status=200,
        media_type=content_type or "application/octet-stream",
    ).with_header("etag", etag)

    if max_age > 0:
        resp = resp.with_header("cache-control", f"public, max-age={max_age}")

    return resp


class StaticFilesModule:
    """Factory for a static-file-serving feature module.

    Call :meth:`for_root` to obtain a ``@module``-decorated class you can
    import into your application module::

        @module(imports=[StaticFilesModule.for_root("/assets", directory="dist")])
        class AppModule:
            pass
    """

    @classmethod
    def for_root(
        cls,
        path: str,
        *,
        directory: str | os.PathLike[str],
        max_age: int = 3600,
        dotfiles: DotfilesPolicy = "allow_well_known",
    ) -> type:
        """Return a configured module that serves *directory* at *path*.

        Parameters
        ----------
        path:
            URL prefix to mount the static tree at (leading slash required).
            Example: ``"/static"`` maps ``/static/css/app.css`` →
            ``<directory>/css/app.css``.
        directory:
            Path to the directory whose contents will be served.  Resolved to
            an absolute path at call time.
        max_age:
            Seconds for ``Cache-Control: public, max-age=<max_age>``.
            Defaults to 3600 (one hour).  Pass ``0`` to omit caching headers.
        dotfiles:
            What to do with paths that have a segment starting with ``.``:
            ``"allow_well_known"`` (default) serves ``/.well-known/**`` and
            answers 404 for every other dotfile path; ``"deny"`` answers 404
            for all of them; ``"allow"`` serves them like any other file.

        Returns
        -------
        type
            A ``@module``-decorated class suitable for
            ``@module(imports=[...])``.
        """
        if dotfiles not in _DOTFILES_POLICIES:
            raise ValueError(
                f"StaticFilesModule.for_root(dotfiles={dotfiles!r}) is not a valid policy; "
                'use "allow_well_known" (default), "deny" or "allow"'
            )
        _base = _FSPath(directory).resolve()
        _prefix = path.rstrip("/") or "/"

        # ------------------------------------------------------------------
        # Generated controller — closed over _base, _prefix, max_age, dotfiles.
        # Two routes:
        #   GET /           → serve index.html at the root of the prefix
        #   GET /{*filepath} → serve any file below the prefix
        # ------------------------------------------------------------------

        @controller(_prefix)
        class _StaticFilesController:
            @get("/", include_in_schema=False)
            async def serve_index(self, request: Request) -> Response:
                return _serve_file(_base, "index.html", request, max_age, dotfiles=dotfiles)

            @get("/{*filepath}", include_in_schema=False)
            async def serve_file(
                self,
                filepath: PathParam[str],  # type: ignore[type-arg]
                request: Request,  # type: ignore[arg-type]
            ) -> Response:
                return _serve_file(_base, filepath, request, max_age, dotfiles=dotfiles)  # type: ignore[arg-type]

        # ------------------------------------------------------------------
        # Generated module — each call returns a unique class so multiple
        # mounts (e.g. "/static" and "/assets") coexist without conflicts.
        # ------------------------------------------------------------------

        @module(controllers=[_StaticFilesController])
        class _ConfiguredStaticFilesModule:
            """Auto-generated static files module."""

        return _ConfiguredStaticFilesModule


__all__ = ["StaticFilesModule"]
