"""Configurable web search. v1 implements the Brave Search API; unconfigured => skipped."""

from __future__ import annotations

from datetime import datetime

import httpx

from brand_evidence.sources.base import (
    RawHit,
    SourceUnavailableError,
    fetch,
    http_kwargs,
    terms_in,
)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


class WebSearchSource:
    name = "web_search"

    def __init__(
        self, provider: str, api_key: str, client: httpx.AsyncClient | None = None
    ) -> None:
        if provider != "brave":
            raise ValueError(f"unsupported web search provider: {provider}")
        self.provider = provider
        self.api_key = api_key
        self.client = client

    async def search(self, terms: list[str], since: datetime) -> list[RawHit]:
        client = self.client or httpx.AsyncClient(**http_kwargs())  # type: ignore[arg-type]
        hits: dict[str, RawHit] = {}
        try:
            for term in terms:
                try:
                    response = await fetch(
                        client,
                        "GET",
                        BRAVE_URL,
                        params={"q": f'"{term}"', "freshness": "pw", "count": 20},
                        headers={
                            "X-Subscription-Token": self.api_key,
                            "Accept": "application/json",
                        },
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise SourceUnavailableError(f"{self.name}: {exc}") from exc
                for item in response.json().get("web", {}).get("results", []):
                    url = item.get("url")
                    if not url or url in hits:
                        continue
                    text = f"{item.get('title', '')} {item.get('description', '')} {url}"
                    matched = terms_in(text, terms) or [term]
                    hits[url] = RawHit(
                        url=url,
                        title=str(item.get("title") or ""),
                        excerpt=str(item.get("description") or ""),
                        published_at=item.get("page_age"),
                        matched_terms=matched,
                    )
        finally:
            if self.client is None:
                await client.aclose()
        return list(hits.values())
