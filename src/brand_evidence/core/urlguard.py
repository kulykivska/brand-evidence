"""Which URLs this tool is willing to fetch.

Every URL on the pull path comes from somewhere else: a search result, an RSS
item, a Reddit post. Handing one of those straight to a browser lets whoever
wrote it choose what the browser opens, so the scheme and the address are
checked first, and again after any redirect.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeUrlError(ValueError):
    """The URL points somewhere this tool will not go."""


def _is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # Unresolvable is not the same as private; let the fetch fail normally.
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return True
    return False


def check_url(url: str, *, resolve: bool = True) -> None:
    """Raise :class:`UnsafeUrlError` unless this URL is safe to fetch.

    ``resolve`` does a DNS lookup to catch a public name pointing at a private
    address, which is how a hostile link reaches a metadata service or an
    internal admin page. Pass False where DNS is unavailable.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        # file:// and data:// would read the machine this runs on and write
        # what they find into the evidence store.
        raise UnsafeUrlError(f"scheme {parts.scheme or '(none)'!r} is not fetchable")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeUrlError("no host in URL")
    if resolve and _is_private(host):
        raise UnsafeUrlError(f"{host} resolves to a private or loopback address")


def is_safe(url: str, *, resolve: bool = True) -> bool:
    try:
        check_url(url, resolve=resolve)
    except UnsafeUrlError:
        return False
    return True
