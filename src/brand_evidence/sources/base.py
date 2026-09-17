"""Common source interface (§7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

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


class SourceUnavailableError(Exception):
    """Raised when a source cannot be reached; the run becomes `partial`, never `failed`."""


def terms_in(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    return [t for t in terms if t.lower() in lowered]
