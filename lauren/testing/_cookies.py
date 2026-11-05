"""A browser-like cookie jar for the in-process test clients.

The jar follows RFC 6265 for storage and matching — host-only vs ``Domain``
cookies, the default ``Path``, ``Max-Age`` winning over ``Expires``, deletion
on an expiry in the past — plus the two behaviours browsers add on top:

* **Potentially trustworthy hosts.** ``Secure`` cookies are stored from, and
  sent to, ``https://`` origins *and* the loopback / test hosts browsers treat
  as secure contexts (``testserver``, ``localhost``, ``*.localhost``,
  ``127.0.0.1``, ``::1``). So an app using ``SessionConfig(secure=True)`` — the
  default — works on the default ``http://testserver`` base URL.
  ``strict=True`` restores RFC-strict behaviour (``Secure`` needs ``https``).
* **Cookie prefixes.** ``__Secure-`` cookies need ``Secure`` from a secure
  context; ``__Host-`` cookies additionally need no ``Domain`` and ``Path=/``.
  A cookie violating its prefix is *rejected*, exactly like a browser, so a
  misconfigured cookie shows up in tests.

``HttpOnly`` and ``SameSite`` are stored for assertions but never affect
sending (the client runs no scripts and every request is same-site).
"""

from __future__ import annotations

import http.cookiejar
import ipaddress
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Mapping

#: Hosts browsers treat as "potentially trustworthy" (secure contexts) over http.
TRUSTWORTHY_HOSTS: frozenset[str] = frozenset({"testserver", "localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class StoredCookie:
    """One cookie held by a :class:`CookieJar`.

    ``expires`` is a Unix timestamp (``None`` for a session cookie);
    ``host_only`` is ``True`` when the cookie was set without a ``Domain``
    attribute and is therefore sent to its exact host only.
    """

    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None
    secure: bool = False
    http_only: bool = False
    same_site: str | None = None
    host_only: bool = True


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _domain_match(host: str, domain: str) -> bool:
    """RFC 6265 §5.1.3 domain matching (``host`` and ``domain`` lower-cased)."""
    if host == domain:
        return True
    return host.endswith("." + domain) and not _is_ip(host)


def _default_path(request_path: str) -> str:
    """RFC 6265 §5.1.4: the "directory" of the request path."""
    if not request_path.startswith("/"):
        return "/"
    cut = request_path.rfind("/")
    return "/" if cut == 0 else request_path[:cut]


def _path_match(request_path: str, cookie_path: str) -> bool:
    """RFC 6265 §5.1.4 path matching."""
    if request_path == cookie_path:
        return True
    if not request_path.startswith(cookie_path):
        return False
    return cookie_path.endswith("/") or request_path[len(cookie_path)] == "/"


def _parse_set_cookie(raw: str) -> tuple[str, str, dict[str, str]] | None:
    """Split a ``Set-Cookie`` value into ``(name, value, attributes)``.

    Attribute names are lower-cased; a flag attribute maps to ``""``. A value
    without ``=`` or with an empty name is ignored (RFC 6265 §5.2).
    """
    pair, *attrs = raw.split(";")
    if "=" not in pair:
        return None
    name, _, value = pair.partition("=")
    name, value = name.strip(), value.strip()
    if not name:
        return None
    parsed: dict[str, str] = {}
    for attr in attrs:
        key, _, val = attr.partition("=")
        key = key.strip().lower()
        if key:
            parsed[key] = val.strip()
    return name, value, parsed


class CookieJar:
    """Cookies stored by a test client and replayed on later requests.

    Cookies are keyed by ``(domain, path, name)``. ``Set-Cookie`` headers on
    every response (redirect hops included) are stored automatically; the
    matching cookies are sent on every request. Use it like a small mapping::

        client.cookies.get("lauren_session")        # value or None
        client.cookies.set("theme", "dark", path="/")
        client.cookies.delete("theme")
        "lauren_session" in client.cookies
        [c.name for c in client.cookies]             # StoredCookie records

    ``clock`` (``time.time`` by default) supplies "now" for expiry, so tests
    can drive expiry deterministically.
    """

    __slots__ = ("_cookies", "_default_host", "_strict", "_clock", "_lock")

    def __init__(
        self,
        *,
        default_host: str = "testserver",
        strict: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cookies: dict[tuple[str, str, str], StoredCookie] = {}
        self._default_host = default_host.lower()
        self._strict = strict
        self._clock = clock
        self._lock = threading.RLock()

    # -- mapping-like surface ---------------------------------------------

    def get(
        self, name: str, default: str | None = None, *, domain: str | None = None, path: str | None = None
    ) -> str | None:
        """Return the value of the first live cookie called ``name`` (or ``default``)."""
        for cookie in self._matching(name, domain, path):
            return cookie.value
        return default

    def set(
        self,
        name: str,
        value: str,
        *,
        domain: str | None = None,
        path: str = "/",
        secure: bool = False,
        http_only: bool = False,
        same_site: str | None = None,
        expires: float | None = None,
    ) -> None:
        """Store a cookie directly (host-only for the client's host unless ``domain`` is given)."""
        host_only = domain is None
        cookie_domain = (domain or self._default_host).lstrip(".").lower()
        with self._lock:
            self._cookies[(cookie_domain, path, name)] = StoredCookie(
                name=name,
                value=value,
                domain=cookie_domain,
                path=path,
                expires=expires,
                secure=secure,
                http_only=http_only,
                same_site=same_site,
                host_only=host_only,
            )

    def delete(self, name: str, *, domain: str | None = None, path: str | None = None) -> None:
        """Remove every cookie called ``name`` (optionally restricted to ``domain`` / ``path``)."""
        with self._lock:
            for cookie in list(self._matching(name, domain, path, include_expired=True)):
                self._cookies.pop((cookie.domain, cookie.path, cookie.name), None)

    def clear(self) -> None:
        """Remove every cookie."""
        with self._lock:
            self._cookies.clear()

    def as_dict(self) -> dict[str, str]:
        """``{name: value}`` for every live cookie (later paths win on name clashes)."""
        return {c.name: c.value for c in self}

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __iter__(self) -> Iterator[StoredCookie]:
        with self._lock:
            self._purge()
            return iter(list(self._cookies.values()))

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._cookies)

    def __repr__(self) -> str:
        return f"CookieJar({self.as_dict()!r})"

    # -- client hooks -------------------------------------------------------

    def is_secure_context(self, scheme: str, host: str) -> bool:
        """Whether ``Secure`` cookies may be stored from / sent to ``scheme://host``."""
        if scheme in ("https", "wss"):
            return True
        if self._strict:
            return False
        host = host.lower()
        return host in TRUSTWORTHY_HOSTS or host.endswith(".localhost")

    def extract(self, set_cookie_values: Iterable[str], *, scheme: str, host: str, path: str) -> None:
        """Store (or delete) the cookies a response set for a request to ``scheme://host/path``."""
        host = host.lower()
        secure_context = self.is_secure_context(scheme, host)
        now = self._clock()
        with self._lock:
            for raw in set_cookie_values:
                self._store_one(raw, host=host, path=path, secure_context=secure_context, now=now)

    def header_value(
        self,
        *,
        scheme: str,
        host: str,
        path: str,
        extra: Mapping[str, str] | None = None,
    ) -> str | None:
        """The ``Cookie`` header for a request, or ``None`` when no cookie applies.

        ``extra`` (the per-request ``cookies=`` argument) is merged over the
        jar for this request only: an entry replaces a stored cookie of the
        same name and is never persisted.
        """
        host = host.lower()
        secure_context = self.is_secure_context(scheme, host)
        with self._lock:
            self._purge()
            selected = [
                c
                for c in self._cookies.values()
                if (c.domain == host if c.host_only else _domain_match(host, c.domain))
                and _path_match(path or "/", c.path)
                and (secure_context or not c.secure)
            ]
        # Longer paths first (RFC 6265 §5.4); ``sorted`` keeps insertion order otherwise.
        selected.sort(key=lambda c: len(c.path), reverse=True)
        pairs = [(c.name, c.value) for c in selected if not extra or c.name not in extra]
        if extra:
            pairs.extend((str(k), str(v)) for k, v in extra.items())
        if not pairs:
            return None
        return "; ".join(f"{k}={v}" for k, v in pairs)

    # -- internals ----------------------------------------------------------

    def _matching(
        self, name: str, domain: str | None, path: str | None, *, include_expired: bool = False
    ) -> Iterator[StoredCookie]:
        with self._lock:
            if not include_expired:
                self._purge()
            candidates = list(self._cookies.values())
        wanted_domain = domain.lstrip(".").lower() if domain is not None else None
        for cookie in candidates:
            if cookie.name != name:
                continue
            if wanted_domain is not None and cookie.domain != wanted_domain:
                continue
            if path is not None and cookie.path != path:
                continue
            yield cookie

    def _purge(self) -> None:
        now = self._clock()
        expired = [key for key, c in self._cookies.items() if c.expires is not None and c.expires <= now]
        for key in expired:
            del self._cookies[key]

    def _store_one(self, raw: str, *, host: str, path: str, secure_context: bool, now: float) -> None:
        parsed = _parse_set_cookie(raw)
        if parsed is None:
            return
        name, value, attrs = parsed
        expires: float | None = None
        if "max-age" in attrs:
            try:
                delta = int(attrs["max-age"])
            except ValueError:
                delta = None
            if delta is not None:
                expires = now + delta if delta > 0 else float("-inf")
        if expires is None and attrs.get("expires"):
            # ``http2time`` parses RFC 1123 and the Netscape cookie date formats.
            expires = http.cookiejar.http2time(attrs["expires"])  # type: ignore[attr-defined]
        domain_attr = attrs.get("domain", "").lstrip(".").lower()
        if domain_attr:
            if not _domain_match(host, domain_attr):
                return  # a cookie for a domain the request host cannot set
            domain, host_only = domain_attr, False
        else:
            domain, host_only = host, True
        path_attr = attrs.get("path", "")
        cookie_path = path_attr if path_attr.startswith("/") else _default_path(path)
        secure = "secure" in attrs
        if secure and not secure_context:
            return  # an insecure origin may not set Secure cookies
        lowered = name.lower()
        if lowered.startswith("__secure-") and not secure:
            return
        if lowered.startswith("__host-") and not (secure and host_only and cookie_path == "/"):
            return
        key = (domain, cookie_path, name)
        if expires is not None and expires <= now:
            self._cookies.pop(key, None)
            return
        same_site = attrs.get("samesite") or None
        self._cookies[key] = StoredCookie(
            name=name,
            value=value,
            domain=domain,
            path=cookie_path,
            expires=expires,
            secure=secure,
            http_only="httponly" in attrs,
            same_site=same_site,
            host_only=host_only,
        )


__all__ = ["CookieJar", "StoredCookie", "TRUSTWORTHY_HOSTS"]
