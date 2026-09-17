from __future__ import annotations

from datetime import UTC, datetime

from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.models import Run
from brand_evidence.digest.builder import build_digest
from brand_evidence.ingest.archive_poll import run_archive_poll
from brand_evidence.ingest.crawler import run_crawl
from brand_evidence.ingest.hook import ingest_post
from tests.factories import BrokenSource, FakeArchive, FakeCapturer, StaticSource, hit

scenarios("daily_cycle.feature")
SINCE = datetime(2026, 9, 1, tzinfo=UTC)


@given("an empty brand-evidence database", target_fixture="world")
def _world(be: App) -> dict:  # type: ignore[type-arg]
    be.archive = FakeArchive()
    return {"be": be, "digest": None}


@when("a Threads post about Northwind is published and hooked")
def _hook(world: dict) -> None:  # type: ignore[type-arg]
    ingest_post(
        world["be"],
        platform="threads",
        url="https://threads.net/@northwind/post/1",
        body="Northwind nailed the Monza podium.",
        published_at="2026-09-14T09:00:00+00:00",
        capturer=FakeCapturer(),
    )


@when("the daily crawl runs with one working source and one broken source")
def _crawl_partial(world: dict) -> None:  # type: ignore[type-arg]
    run_crawl(
        world["be"],
        since=SINCE,
        capturer=FakeCapturer(),
        robots_check=lambda _u: True,
        sources=[
            StaticSource("hn_algolia", [hit("https://news.ycombinator.com/item?id=7")]),
            BrokenSource(),
        ],
    )


@when("the daily crawl runs with sources that find nothing")
def _crawl_empty(world: dict) -> None:  # type: ignore[type-arg]
    run_crawl(world["be"], since=SINCE, sources=[StaticSource("hn_algolia", [])], capture=False)


@when("the archive poll runs and the archive confirms the post snapshot")
def _poll(world: dict) -> None:  # type: ignore[type-arg]
    world[
        "be"
    ].archive.check_result = (
        "https://web.archive.org/web/20260914090500/https://threads.net/@northwind/post/1"
    )
    run_archive_poll(world["be"])


@when("the daily digest is built")
def _digest(world: dict) -> None:  # type: ignore[type-arg]
    world["digest"] = build_digest(world["be"]).markdown


@then(parsers.parse('the digest lists the post with archive status "{status}"'))
def _post_status(world: dict, status: str) -> None:  # type: ignore[type-arg]
    assert f"| threads | https://threads.net/@northwind/post/1 | {status}" in world["digest"]


@then(parsers.parse("the digest lists {count:d} new mention"))
def _mentions(world: dict, count: int) -> None:  # type: ignore[type-arg]
    assert world["digest"].count("- **hn_algolia**") == count


@then(parsers.parse('the digest names the failed source "{name}"'))
def _failed(world: dict, name: str) -> None:  # type: ignore[type-arg]
    assert f"failed sources: {name}" in world["digest"]


@then(parsers.parse('the crawl run is recorded as "{status}"'))
def _run_status(world: dict, status: str) -> None:  # type: ignore[type-arg]
    with world["be"].sessions() as s:
        assert s.scalars(select(Run).where(Run.job == "crawl")).one().status == status


@then(parsers.parse("the evidence chain verifies with {count:d} entries"))
def _chain(world: dict, count: int) -> None:  # type: ignore[type-arg]
    with world["be"].sessions() as s:
        result = evidence_log.verify(s)
    assert result.ok and result.entries == count, result


@then(parsers.parse('the digest states "{text}"'))
def _states(world: dict, text: str) -> None:  # type: ignore[type-arg]
    assert text in world["digest"]
