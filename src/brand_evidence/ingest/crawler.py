"""Pull path: query every enabled source, dedupe, capture new mentions, archive them."""

from __future__ import annotations

import asyncio
import urllib.robotparser
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.capture.archiver import RateLimiter
from brand_evidence.capture.screenshot import Capturer
from brand_evidence.core import evidence_log
from brand_evidence.core.clock import now_iso
from brand_evidence.core.db import session_scope
from brand_evidence.core.ids import uuid7
from brand_evidence.core.logging import get_logger
from brand_evidence.core.models import Mention
from brand_evidence.core.runs import RunContext, tracked_run
from brand_evidence.core.urlguard import is_safe
from brand_evidence.core.urlnorm import normalize_url
from brand_evidence.ingest.capture_pipeline import make_capturer, store_artifacts, submit_archive
from brand_evidence.ingest.publications_sync import sync_publications
from brand_evidence.sources.base import RawHit, Source, SourceUnavailableError, user_agent
from brand_evidence.sources.registry import build_sources

log = get_logger(__name__)


async def gather_hits(
    sources: list[Source], terms: list[str], since: datetime, ctx: RunContext
) -> list[tuple[str, RawHit]]:
    async def one(source: Source) -> list[tuple[str, RawHit]]:
        try:
            hits = await source.search(terms, since)
            ctx.stats.setdefault("hits_by_source", {})[source.name] = len(hits)
            return [(source.name, h) for h in hits]
        except SourceUnavailableError as exc:
            ctx.mark_partial(str(exc))
            ctx.stats.setdefault("failed_sources", []).append(source.name)
            log.warning("source_failed", source=source.name, error=str(exc))
            return []
        except Exception as exc:  # noqa: BLE001 - one bad source must not sink the run
            ctx.mark_partial(f"{source.name}: {exc!r}")
            ctx.stats.setdefault("failed_sources", []).append(source.name)
            log.error("source_crashed", source=source.name, error=repr(exc))
            return []

    results = await asyncio.gather(*(one(s) for s in sources))
    return [pair for batch in results for pair in batch]


def robots_allows(url: str, client: httpx.Client | None = None) -> bool:
    # An unfetchable scheme used to reach the caller as an HTTPError below,
    # whose handler answers "allowed".
    if not is_safe(url, resolve=False):
        return False
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    try:
        own = client is None
        client = client or httpx.Client(timeout=15, follow_redirects=True)
        response = client.get(robots_url, headers={"User-Agent": user_agent()})
        if own:
            client.close()
        if response.status_code >= 400:
            return True
        parser.parse(response.text.splitlines())
    except httpx.HTTPError:
        return True
    return parser.can_fetch(user_agent().split("/")[0], url)


def run_crawl(
    app: App,
    *,
    since: datetime | None = None,
    only_source: str | None = None,
    sources: list[Source] | None = None,
    capturer: Capturer | None = None,
    capture: bool = True,
    robots_check: Callable[[str, httpx.Client | None], bool] = robots_allows,
) -> str:
    """Execute one crawl run; returns the run id."""
    since = since or datetime.now(UTC) - timedelta(days=2)
    with tracked_run(app.sessions, "crawl") as ctx:
        if app.settings.own_publications_enabled and not only_source:
            sync_publications(
                app,
                ctx,
                capturer=capturer or (make_capturer(app) if capture else None),
                capture=capture,
            )
        if sources is None:
            sources, skipped = build_sources(app.settings, app.sources_config)
            ctx.stats["skipped_sources"] = skipped
        if only_source:
            sources = [s for s in sources if s.name == only_source]
        ctx.stats["sources"] = [s.name for s in sources]
        ctx.stats["since"] = since.isoformat()

        pairs = asyncio.run(gather_hits(sources, app.terms, since, ctx))
        new_ids = _dedupe_and_store(app, pairs, ctx)
        ctx.stats["new_mentions"] = len(new_ids)

        if capture and new_ids:
            _capture_new(app, new_ids, ctx, capturer or make_capturer(app), robots_check)
        return ctx.run_id


def _dedupe_and_store(
    app: App, pairs: list[tuple[str, RawHit]], ctx: RunContext
) -> list[tuple[str, str]]:
    new: list[tuple[str, str]] = []
    updated = 0
    seen_this_run: dict[str, Mention] = {}
    with session_scope(app.sessions) as session:
        for source_name, hit in pairs:
            norm = normalize_url(hit.url)
            existing = (
                seen_this_run.get(norm)
                or session.scalars(select(Mention).where(Mention.url == norm)).first()
            )
            if existing:
                merged = sorted(set(existing.matched_terms) | set(hit.matched_terms))
                if merged != existing.matched_terms:
                    existing.matched_terms = merged
                    evidence_log.append(
                        session,
                        "mention.terms_updated",
                        {"id": existing.id, "matched_terms": merged, "source": source_name},
                    )
                    updated += 1
                continue
            mention = Mention(
                id=uuid7(),
                source=source_name,
                url=norm,
                title=hit.title,
                excerpt=hit.excerpt,
                author=hit.author,
                published_at=hit.published_at,
                discovered_at=now_iso(),
                matched_terms=sorted(set(hit.matched_terms)),
                status="new",
            )
            evidence_log.record(session, "mention.created", mention)
            seen_this_run[norm] = mention
            new.append((mention.id, norm))
    ctx.stats["updated_mentions"] = updated
    return new


def _capture_new(app, new_ids, ctx, capturer, robots_check) -> None:  # type: ignore[no-untyped-def]
    limiter = RateLimiter(app.settings.archive_min_interval_seconds)
    skip_hosts = set(app.sources_config.skip_capture_hosts)
    budget = app.settings.max_captures_per_run
    captured = 0
    for mention_id, url in new_ids:
        if captured >= budget:
            ctx.stats["capture_budget_exhausted"] = True
            break
        # hostname, not netloc: netloc carries the port and any userinfo, so
        # example.com:8443 slipped past a skip list naming example.com.
        host = (urlsplit(url).hostname or "").lower()
        if host in skip_hosts or not robots_check(url):
            ctx.stats.setdefault("capture_skipped", []).append(url)
            continue
        try:
            result = capturer.capture_url(url)
        except Exception as exc:  # noqa: BLE001
            ctx.mark_partial(f"capture failed for {url}: {exc}")
            continue
        with session_scope(app.sessions) as session:
            store_artifacts(app, session, "mention", mention_id, result)
            submit_archive(app, session, "mention", mention_id, url, limiter)
        captured += 1
    ctx.stats["captured"] = captured
