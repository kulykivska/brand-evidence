"""Google Alerts delivered as Atom feeds. Configure the feed URLs once in BE_GOOGLE_ALERTS_FEEDS."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import httpx
from defusedxml.ElementTree import fromstring as safe_fromstring

from brand_evidence.core.clock import parse_iso
from brand_evidence.core.logging import get_logger
from brand_evidence.sources.base import (
    RawHit,
    SourceUnavailableError,
    fetch,
    http_kwargs,
    terms_in,
)

log = get_logger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"


class GoogleAlertsRSSSource:
    name = "google_alerts_rss"

    def __init__(self, feeds: list[str], client: httpx.AsyncClient | None = None) -> None:
        self.feeds = feeds
        self.client = client

    async def search(self, terms: list[str], since: datetime) -> list[RawHit]:
        client = self.client or httpx.AsyncClient(**http_kwargs())  # type: ignore[arg-type]
        hits: list[RawHit] = []
        try:
            for feed in self.feeds:
                try:
                    response = await fetch(client, "GET", feed)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    # Not str(exc): httpx spells out the full feed URL, and a
                    # Google Alerts URL is itself the credential.
                    raise SourceUnavailableError(
                        f"{self.name}: {type(exc).__name__} fetching a configured feed"
                    ) from exc
                hits.extend(parse_atom(response.text, terms, since))
        finally:
            if self.client is None:
                await client.aclose()
        return hits


def parse_atom(xml_text: str, terms: list[str], since: datetime) -> list[RawHit]:
    root = safe_fromstring(xml_text)
    hits: list[RawHit] = []
    for entry in root.iter(f"{ATOM}entry"):
        link_el = entry.find(f"{ATOM}link")
        href = link_el.get("href", "") if link_el is not None else ""
        url = _unwrap_google_redirect(href)
        if not url:
            continue
        title = _strip_tags(_all_text(entry.find(f"{ATOM}title")))
        content = _strip_tags(_all_text(entry.find(f"{ATOM}content")))
        published = entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")
        if published:
            try:
                if parse_iso(published) < since:
                    continue
            except ValueError:
                # Unparseable date: keep the entry rather than drop it, but
                # say so, or a format change re-ingests the whole backlog.
                log.warning("alerts_date_unparsed", published=published)
        matched = terms_in(f"{title} {content}", terms)
        hits.append(
            RawHit(
                url=url,
                title=title,
                excerpt=content,
                published_at=published,
                matched_terms=matched or [terms[0]] if terms else matched,
            )
        )
    return hits


def _all_text(element: object) -> str:
    if element is None:
        return ""
    return " ".join(part.strip() for part in element.itertext() if part.strip())  # type: ignore[attr-defined]


def _unwrap_google_redirect(href: str) -> str:
    parts = urlsplit(href)
    if parts.netloc.endswith("google.com") and parts.path == "/url":
        target = parse_qs(parts.query).get("url") or parse_qs(parts.query).get("q")
        if target:
            return target[0]
    return href


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()
