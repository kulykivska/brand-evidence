from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from brand_evidence.sources.base import SourceUnavailableError
from brand_evidence.sources.google_alerts_rss import parse_atom
from brand_evidence.sources.hn_algolia import HNAlgoliaSource

SINCE = datetime(2026, 9, 1, tzinfo=UTC)


@respx.mock
async def test_hn_returns_matching_hits_only() -> None:
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "objectID": "1",
                        "title": "Northwind predicts Monza",
                        "author": "a",
                        "created_at": "2026-09-10T00:00:00Z",
                    },
                    {
                        "objectID": "2",
                        "title": "Unrelated",
                        "author": "b",
                        "created_at": "2026-09-10T00:00:00Z",
                    },
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        hits = await HNAlgoliaSource(client).search(["Northwind"], SINCE)
    assert [h.url for h in hits] == ["https://news.ycombinator.com/item?id=1"]
    assert hits[0].matched_terms == ["Northwind"]


@respx.mock
async def test_hn_network_error_becomes_source_unavailable() -> None:
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        side_effect=httpx.ConnectError("down")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceUnavailableError):
            await HNAlgoliaSource(client).search(["Northwind"], SINCE)


def test_google_alerts_atom_unwraps_redirects() -> None:
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>North Wind launches widget</title>
        <link href="https://www.google.com/url?rct=j&amp;url=https://news.example.com/a&amp;ct=ga"/>
        <published>2026-09-10T10:00:00Z</published>
        <content>Some <b>North Wind</b> news</content>
      </entry>
      <entry>
        <title>Old</title>
        <link href="https://news.example.com/old"/>
        <published>2020-01-01T00:00:00Z</published>
        <content>North Wind</content>
      </entry>
    </feed>"""
    hits = parse_atom(xml, ["Northwind", "North Wind"], SINCE)
    assert len(hits) == 1
    assert hits[0].url == "https://news.example.com/a"
    assert hits[0].excerpt == "Some North Wind news"
    assert hits[0].matched_terms == ["North Wind"]
