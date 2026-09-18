"""Pull path: query every enabled source, dedupe, capture new mentions, archive them."""

from __future__ import annotations

import asyncio
import urllib.robotparser
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

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


# What a robots.txt fetch settled. UNAVAILABLE is not DENY: the host owes us an
# answer it did not give, so the mention waits for the next run rather than
# being written off.
ALLOW, DENY, UNAVAILABLE = "allow", "deny", "unavailable"

# RFC 9309 2.3.1.3: these mean "unavailable", and unavailable means do not crawl.
UNAVAILABLE_STATUS = (429,)


class RobotsCache:
    """One robots.txt fetch per host, over one connection, for a whole run.

    Ten mentions on one site used to mean ten requests and ten TCP connections,
    and the host answering the later ones with 429 read as "allowed", which is
    not what it said.
    """

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._own = client is None
        self._client = client or httpx.Client(timeout=15, follow_redirects=True)
        # None means "fetched, and it does not restrict us".
        self._hosts: dict[
            tuple[str, str, int | None], urllib.robotparser.RobotFileParser | None | str
        ] = {}

    def verdict(self, url: str) -> str:
        # Resolving, not just parsing: this is the first request the tool makes
        # for a URL somebody else wrote, so a public name pointing at a private
        # address must be refused before the fetch, not before the capture.
        if not is_safe(url):
            return DENY
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        # Keyed on the host itself: userinfo in the URL is both a credential and,
        # left in the key, a way for a feed to force one fetch per URL.
        key = (parts.scheme, host, parts.port)
        origin = f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
        if key not in self._hosts:
            self._hosts[key] = self._fetch(origin)
        parser = self._hosts[key]
        if parser == UNAVAILABLE:
            return UNAVAILABLE
        if parser is None or isinstance(parser, str):
            return ALLOW
        return ALLOW if parser.can_fetch(user_agent().split("/")[0], url) else DENY

    def allows(self, url: str) -> bool:
        return self.verdict(url) == ALLOW

    def _fetch(self, origin: str) -> urllib.robotparser.RobotFileParser | None | str:
        try:
            response = self._client.get(
                f"{origin}/robots.txt", headers={"User-Agent": user_agent()}
            )
        except httpx.HTTPError as exc:
            log.warning("robots_fetch_failed", origin=origin, error=str(exc))
            return UNAVAILABLE
        if response.status_code in UNAVAILABLE_STATUS or response.status_code >= 500:
            log.warning("robots_unavailable", origin=origin, status=response.status_code)
            return UNAVAILABLE
        if response.status_code >= 400:
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    def close(self) -> None:
        if self._own:
            self._client.close()


def robots_allows(url: str, client: httpx.Client | None = None) -> bool:
    """One-off check. Anything looping over URLs wants RobotsCache instead."""
    cache = RobotsCache(client)
    try:
        return cache.allows(url)
    finally:
        cache.close()


def run_crawl(
    app: App,
    *,
    since: datetime | None = None,
    only_source: str | None = None,
    sources: list[Source] | None = None,
    capturer: Capturer | None = None,
    capture: bool = True,
    robots_check: Callable[[str], bool | str] | None = None,
) -> str:
    """Execute one crawl run; returns the run id."""
    since = since or datetime.now(UTC) - timedelta(days=2)
    with tracked_run(app.sessions, "crawl") as ctx, ExitStack() as closing:
        if robots_check is None:
            robots = RobotsCache()
            closing.callback(robots.close)
            robots_check = robots.verdict
        if capture and capturer is None:
            capturer = make_capturer(app)
            closing.callback(capturer.close)
        if app.settings.own_publications_enabled and not only_source:
            sync_publications(app, ctx, capturer=capturer, capture=capture)
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

        if capture and capturer is not None:
            # Not only new_ids: anything still owed a capture from an
            # earlier run is picked up here too.
            _capture_pending(app, ctx, capturer, robots_check)
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


# A capture that keeps throwing is usually a page that will never load.
# Stop after this many tries so one bad URL cannot eat every run's budget.
MAX_CAPTURE_ATTEMPTS = 3

# What Mention.capture_state can hold. PENDING and nothing else is retried.
PENDING, CAPTURED, SKIPPED, FAILED = "pending", "captured", "skipped", "failed"
CAPTURE_STATES = (PENDING, CAPTURED, SKIPPED, FAILED)


def _pending_captures(session: Session, budget: int) -> list[tuple[str, str]]:
    """Mentions still owed a capture, oldest first.

    Not just the ones this run discovered: a mention the budget cut off, or
    whose capture threw, used to be dropped forever because the next run
    only ever looked at its own new rows."""
    rows = session.scalars(
        select(Mention)
        .where(Mention.capture_state == PENDING)
        .order_by(Mention.discovered_at)
        .limit(budget)
    ).all()
    return [(m.id, m.url) for m in rows]


def _record_capture_state(session: Session, mention_id: str, state: str) -> None:
    """Move a mention's capture state and say so in the evidence log: a decision
    that stops a mention being captured is part of the record."""
    mention = session.get(Mention, mention_id)
    if mention is None:
        return
    mention.capture_state = state
    evidence_log.append(
        session,
        "mention.capture_state",
        {"id": mention_id, "capture_state": state, "capture_attempts": mention.capture_attempts},
    )


def _set_capture_state(app: App, mention_id: str, state: str) -> None:
    with session_scope(app.sessions) as session:
        _record_capture_state(session, mention_id, state)


def _capture_pending(  # noqa: PLR0912
    app: App,
    ctx: RunContext,
    capturer: Capturer,
    robots_check: Callable[[str], bool | str],
) -> None:
    limiter = RateLimiter(app.settings.archive_min_interval_seconds)
    skip_hosts = set(app.sources_config.skip_capture_hosts)
    budget = app.settings.max_captures_per_run
    with app.sessions() as session:
        pending = _pending_captures(session, budget)
    captured = 0
    for mention_id, url in pending:
        # hostname, not netloc: netloc carries the port and any userinfo, so
        # example.com:8443 slipped past a skip list naming example.com.
        host = (urlsplit(url).hostname or "").lower()
        if host in skip_hosts:
            _set_capture_state(app, mention_id, SKIPPED)
            ctx.stats.setdefault("capture_skipped", []).append(url)
            continue
        verdict = _verdict(robots_check, url)
        if verdict == UNAVAILABLE:
            # The host owes an answer it did not give. Leave the mention
            # pending rather than writing it off on a 429.
            ctx.stats["robots_unavailable"] = ctx.stats.get("robots_unavailable", 0) + 1
            ctx.mark_partial(f"robots.txt unavailable for {url}")
            continue
        if verdict == DENY:
            _set_capture_state(app, mention_id, SKIPPED)
            ctx.stats.setdefault("capture_skipped", []).append(url)
            continue
        try:
            result = capturer.capture_url(url)
        except Exception as exc:  # noqa: BLE001 - reported on the run
            with session_scope(app.sessions) as session:
                mention = session.get(Mention, mention_id)
                attempts = (mention.capture_attempts if mention else 0) + 1
                if mention is not None:
                    mention.capture_attempts = attempts
                    if attempts >= MAX_CAPTURE_ATTEMPTS:
                        _record_capture_state(session, mention_id, FAILED)
            ctx.mark_partial(
                f"capture failed for {url} (attempt {attempts}"
                f"/{MAX_CAPTURE_ATTEMPTS}): {exc}"
            )
            continue
        # One transaction: artifacts written but state left pending meant the
        # next run captured and archived the same URL again.
        with session_scope(app.sessions) as session:
            store_artifacts(app, session, "mention", mention_id, result)
            submit_archive(app, session, "mention", mention_id, url, limiter)
            _record_capture_state(session, mention_id, CAPTURED)
        captured += 1
    ctx.stats["captured"] = captured
    with app.sessions() as session:
        still_pending = (
            session.scalar(
                select(func.count()).select_from(Mention).where(Mention.capture_state == PENDING)
            )
            or 0
        )
    if still_pending:
        # Named so the digest can say it: these are owed a capture, and the
        # next run will take them.
        ctx.stats["capture_pending"] = still_pending


def _verdict(check: Callable[[str], bool | str], url: str) -> str:
    """Accept a plain allow/deny predicate as well as the three-way verdict."""
    answer = check(url)
    if answer is True:
        return ALLOW
    if answer is False:
        return DENY
    return answer
