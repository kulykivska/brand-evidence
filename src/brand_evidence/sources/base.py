"""Common source interface (§7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

from brand_evidence import __version__

EXCERPT_MAX = 2000

# The contact URL goes in the User-Agent of every request this tool makes.
# There is no default: naming anyone here would attribute a user's crawling
# to them. The composition root sets it from BE_CONTACT_URL.
_contact_url = ""


def set_contact_url(url: str) -> None:
    global _contact_url
    _contact_url = url.strip()


def user_agent() -> str:
    """Identify this tool, and whoever is running it when they said so."""
    if _contact_url:
        return f"brand-evidence/{__version__} (+{_contact_url}; record-keeping bot)"
    return f"brand-evidence/{__version__} (record-keeping bot)"


# A feed that serves gigabytes - hostile or merely misconfigured - used to
# OOM-kill the scheduled job, leaving its run row "running" forever.
MAX_RESPONSE_BYTES = 25 * 1024 * 1024


class SourceUnavailableError(Exception):
    """Raised when a source cannot be reached; the run becomes `partial`, never `failed`."""


class ResponseTooLargeError(SourceUnavailableError):
    """The peer sent, or promised, more body than this tool will read."""


def http_kwargs(timeout: float = 30.0) -> dict[str, object]:
    """Shared client settings. Bodies are bounded by `fetch`, not by a hook."""
    return {
        "timeout": timeout,
        "headers": {"User-Agent": user_agent()},
        "follow_redirects": True,
    }


# Rebuilding the response from the bytes we read means dropping the headers that
# describe the wire body, which is no longer what the caller holds.
_WIRE_HEADERS = ("content-length", "content-encoding", "transfer-encoding")


async def fetch(client: Any, method: str, url: str, **kwargs: Any) -> Any:
    """One request whose body is read up to MAX_RESPONSE_BYTES and no further.

    A declared length is refused before anything is read; a response that
    declares nothing - the shape a hostile endpoint sends - is counted as it
    streams and abandoned at the limit.
    """
    async with client.stream(method, url, **kwargs) as streamed:
        declared = streamed.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_RESPONSE_BYTES):
            raise ResponseTooLargeError(
                f"{url} declares {declared!r} bytes, over the {MAX_RESPONSE_BYTES} limit"
            )
        total = 0
        chunks: list[bytes] = []
        async for chunk in streamed.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ResponseTooLargeError(
                    f"{url} sent more than the {MAX_RESPONSE_BYTES} byte limit"
                )
            chunks.append(chunk)
        headers = [
            (k, v) for k, v in streamed.headers.multi_items() if k.lower() not in _WIRE_HEADERS
        ]
        return httpx.Response(
            streamed.status_code,
            headers=headers,
            content=b"".join(chunks),
            request=streamed.request,
        )


@dataclass
class RawHit:
    url: str
    title: str = ""
    excerpt: str = ""
    author: str | None = None
    published_at: str | None = None
    matched_terms: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.excerpt = (self.excerpt or "")[:EXCERPT_MAX]
        self.title = (self.title or "")[:500]


class Source(Protocol):
    name: str

    async def search(self, terms: list[str], since: datetime) -> list[RawHit]: ...


def terms_in(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    return [t for t in terms if t.lower() in lowered]
