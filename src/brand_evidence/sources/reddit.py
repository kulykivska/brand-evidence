"""Reddit search, read-only, via OAuth client-credentials (script app)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from brand_evidence.sources.base import (
    RawHit,
    SourceUnavailableError,
    fetch,
    http_kwargs,
    terms_in,
)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 - a URL, not a secret
SEARCH_URL = "https://oauth.reddit.com/search"


class RedditSource:
    name = "reddit"

    def __init__(
        self, client_id: str, client_secret: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.client = client

    async def _token(self, client: httpx.AsyncClient) -> str:
        response = await fetch(
            client,
            "POST",
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )
        response.raise_for_status()
        return str(response.json()["access_token"])

    async def search(self, terms: list[str], since: datetime) -> list[RawHit]:
        client = self.client or httpx.AsyncClient(**http_kwargs())  # type: ignore[arg-type]
        hits: dict[str, RawHit] = {}
        try:
            try:
                token = await self._token(client)
                headers = {"Authorization": f"bearer {token}"}
                for term in terms:
                    response = await fetch(
                        client,
                        "GET",
                        SEARCH_URL,
                        params={"q": f'"{term}"', "sort": "new", "limit": 100, "type": "link"},
                        headers=headers,
                    )
                    response.raise_for_status()
                    for child in response.json().get("data", {}).get("children", []):
                        hit = _to_hit(child.get("data", {}), terms, since)
                        if hit and hit.url not in hits:
                            hits[hit.url] = hit
            except httpx.HTTPError as exc:
                raise SourceUnavailableError(f"{self.name}: {exc}") from exc
        finally:
            if self.client is None:
                await client.aclose()
        return list(hits.values())


def _to_hit(data: dict, terms: list[str], since: datetime) -> RawHit | None:  # type: ignore[type-arg]
    created = data.get("created_utc")
    if created is None:
        return None
    published = datetime.fromtimestamp(float(created), tz=UTC)
    if published < since:
        return None
    permalink = data.get("permalink")
    if not permalink:
        return None
    text = f"{data.get('title', '')} {data.get('selftext', '')} {data.get('url', '')}"
    matched = terms_in(text, terms)
    if not matched:
        return None
    return RawHit(
        url=f"https://www.reddit.com{permalink}",
        title=str(data.get("title") or ""),
        excerpt=str(data.get("selftext") or data.get("url") or ""),
        author=data.get("author"),
        published_at=published.isoformat(timespec="seconds"),
        matched_terms=matched,
    )
