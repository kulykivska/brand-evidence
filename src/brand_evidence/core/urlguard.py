"""Which URLs this tool is willing to fetch.

Every URL on the pull path comes from somewhere else: a search result, an RSS
item, a Reddit post. Handing one of those straight to a browser lets whoever
wrote it choose what the browser opens, so the scheme and the address are
checked first, and again on every hop.

The checks are the `ssrf-guard` package. This module keeps the names the rest
of the tool imports and adds the one rule that is about browsers: backslashes.
"""

from __future__ import annotations

from typing import Any

import httpx
import ssrf_guard
from ssrf_guard import Policy, Resolver, UnsafeUrlError, addresses_for
from ssrf_guard.core import Address

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_REDIRECTS = 5

GUARD_POLICY = Policy(schemes=ALLOWED_SCHEMES)
LOCAL_POLICY = Policy(schemes=ALLOWED_SCHEMES, allow_private=True)

__all__ = [
    "ALLOWED_SCHEMES",
    "GUARD_POLICY",
    "MAX_REDIRECTS",
    "Address",
    "GuardedTransport",
    "Resolver",
    "UnsafeUrlError",
    "addresses_for",
    "check_url",
    "is_safe",
    "resolve_host",
]


def check_url(url: str, *, resolve: bool = True) -> None:
    """Raise :class:`UnsafeUrlError` unless this URL is safe to fetch.

    ``resolve`` does a DNS lookup to catch a public name pointing at a private
    address. This checks a string; only a pinned connection closes rebinding.
    """
    if "\\" in url:
        # Python reads http://127.0.0.1\@evil.com as evil.com; a browser as 127.0.0.1.
        raise UnsafeUrlError("backslash in URL")
    policy = GUARD_POLICY if resolve else Policy(schemes=ALLOWED_SCHEMES, resolve=False)
    ssrf_guard.check_url(url, policy)


def is_safe(url: str, *, resolve: bool = True) -> bool:
    try:
        check_url(url, resolve=resolve)
    except UnsafeUrlError:
        return False
    return True


def resolve_host(host: str, resolver: Resolver = addresses_for) -> list[Address]:
    """Resolve once; the addresses to connect to, or UnsafeUrlError."""
    return ssrf_guard.resolve_host(host, GUARD_POLICY, resolver)


# The httpx bases are for the type checker; their __enter__ signatures differ in return type.
class GuardedTransport(  # type: ignore[misc]
    ssrf_guard.GuardedTransport, httpx.BaseTransport, httpx.AsyncBaseTransport
):
    """ssrf-guard's transport: every hop checked, every connection pinned to
    the address that was checked. ``allow_local`` is for URLs the owner wrote."""

    def __init__(
        self, inner: Any = None, *, allow_local: bool = False, resolver: Resolver | None = None
    ) -> None:
        super().__init__(inner, LOCAL_POLICY if allow_local else GUARD_POLICY, resolver)

    def _check(self, request: Any) -> None:
        if "\\" in str(request.url):
            raise UnsafeUrlError("backslash in URL")
        super()._check(request)
