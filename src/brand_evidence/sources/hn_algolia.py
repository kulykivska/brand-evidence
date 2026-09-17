"""Hacker News via the Algolia search API. Free, no key."""

from __future__ import annotations

from datetime import datetime

import httpx

from brand_evidence.sources.base import RawHit, SourceUnavailableError, terms_in, user_agent

ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"


class HNAlgoliaSource:
    name = "hn_algolia"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, terms: list[str], since: datetime) -> list[RawHit]:
        client = self.client or httpx.AsyncClient(timeout=30, headers={"User-Agent": user_agent()})
        hits: dict[str, RawHit] = {}
        try:
            for term in terms:
                params: dict[str, str] = {
                    "query": f'"{term}"',
                    "numericFilters": f"created_at_i>{int(since.timestamp())}",
                    "hitsPerPage": "50",
                }
                try:
                    response = await client.get(ENDPOINT, params=params)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise SourceUnavailableError(f"{self.name}: {exc}") from exc
                for item in response.json().get("hits", []):
                    hit = _to_hit(item, terms)
                    if hit is None:
                        continue
                    if hit.url in hits:
                        hits[hit.url].matched_terms = sorted(
                            set(hits[hit.url].matched_terms) | set(hit.matched_terms)
                        )
                    else:
                        hits[hit.url] = hit
        finally:
            if self.client is None:
                await client.aclose()
        return list(hits.values())


def _to_hit(item: dict, terms: list[str]) -> RawHit | None:  # type: ignore[type-arg]
    object_id = item.get("objectID")
    if not object_id:
        return None
    hn_url = f"https://news.ycombinator.com/item?id={object_id}"
    text = " ".join(
        str(item.get(k) or "")
        for k in ("title", "story_title", "comment_text", "story_text", "url")
    )
    matched = terms_in(text, terms)
    if not matched:
        return None
    return RawHit(
        url=hn_url,
        title=str(item.get("title") or item.get("story_title") or ""),
        excerpt=_strip_tags(
            str(item.get("comment_text") or item.get("story_text") or item.get("url") or "")
        ),
        author=item.get("author"),
        published_at=item.get("created_at"),
        matched_terms=matched,
    )


def _strip_tags(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", " ", text).strip()
