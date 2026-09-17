from __future__ import annotations

import httpx
import respx
from sqlalchemy import select

from brand_evidence.app import App, build_app
from brand_evidence.core.models import Post, Run
from brand_evidence.core.runs import tracked_run
from brand_evidence.ingest.crawler import run_crawl
from brand_evidence.ingest.publications_sync import sync_publications
from tests.conftest import make_settings
from tests.factories import FakeArchive, FakeCapturer, StaticSource

FEED = "https://api.northwind.example/admin/news/publications"


def _feed_app(tmp_path) -> App:  # type: ignore[no-untyped-def]
    settings = make_settings(tmp_path, own_publications_url=FEED, own_publications_token="k1")
    app = build_app(settings, create_schema=True)
    app.archive = FakeArchive()
    return app


@respx.mock
def test_sync_records_new_posts_and_skips_known(tmp_path) -> None:  # type: ignore[no-untyped-def]
    be = _feed_app(tmp_path)
    route = respx.get(FEED).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "platform": "linkedin",
                        "external_url": "https://linkedin.com/posts/rm-1",
                        "external_id": "e1",
                        "published_at": "2026-09-14T09:00:00+00:00",
                        "text": "Northwind podium call",
                    },
                    {
                        "platform": "reddit",
                        "external_url": None,
                        "published_at": "2026-09-14T09:05:00+00:00",
                    },
                    {
                        "platform": "reddit",
                        "external_url": "https://reddit.com/r/f1/x",
                        "published_at": "2026-09-14T10:00:00+00:00",
                        "text": "manual",
                    },
                ]
            },
        )
    )
    with tracked_run(be.sessions, "sync_posts") as ctx:
        created = sync_publications(be, ctx, capturer=FakeCapturer(), client=httpx.Client())
    assert created == 2
    assert route.calls[0].request.headers["X-API-Key"] == "k1"
    assert "since" not in str(route.calls[0].request.url)
    with be.sessions() as s:
        posts = s.scalars(select(Post).order_by(Post.published_at)).all()
        assert [p.platform for p in posts] == ["linkedin", "other"]
        assert posts[0].source_path == "feed" and posts[0].brand_mentioned

    with tracked_run(be.sessions, "sync_posts") as ctx:
        created = sync_publications(be, ctx, capturer=FakeCapturer(), client=httpx.Client())
    assert created == 0 and ctx.stats["own_publications"]["known"] == 2
    assert "since=2026-09-14T10" in str(route.calls[1].request.url)


@respx.mock
def test_feed_outage_makes_crawl_partial_not_failed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    be = _feed_app(tmp_path)
    respx.get(FEED).mock(side_effect=httpx.ConnectError("down"))
    run_id = run_crawl(be, sources=[StaticSource("a", [])], capture=False)
    with be.sessions() as s:
        run = s.get(Run, run_id)
        assert run is not None and run.status == "partial"
        assert "error" in run.stats["own_publications"]
