"""Standing report for the interface layer: did / found / need, as JSON. Facts only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.models import ArchiveSnapshot, Mention, Post, Run


def build_report(app: App, *, project: str | None = None, hours: int = 24) -> dict[str, Any]:
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    stale_cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    with app.sessions() as s:
        runs = s.scalars(select(Run).where(Run.started_at >= since, Run.status != "running")).all()
        posts = (
            s.scalar(select(func.count()).select_from(Post).where(Post.ingested_at >= since)) or 0
        )
        confirmed = (
            s.scalar(
                select(func.count())
                .select_from(ArchiveSnapshot)
                .where(ArchiveSnapshot.confirmed_at >= since)
            )
            or 0
        )
        new_mentions = s.scalars(select(Mention).where(Mention.discovered_at >= since)).all()
        stale = (
            s.scalar(
                select(func.count())
                .select_from(Mention)
                .where(Mention.status == "new", Mention.discovered_at < stale_cutoff)
            )
            or 0
        )
        failed_archives = (
            s.scalar(
                select(func.count())
                .select_from(ArchiveSnapshot)
                .where(ArchiveSnapshot.status == "failed")
            )
            or 0
        )
        chain = evidence_log.verify(s)
    failed_sources = sorted(
        {src for r in runs for src in (r.stats or {}).get("failed_sources", [])}
    )
    failed_runs = [f"{r.job} run failed" for r in runs if r.status == "failed"]
    need: list[str] = []
    if stale:
        need.append(f"{stale} mention(s) awaiting triage for more than 7 days")
    if failed_archives:
        need.append(f"{failed_archives} archive snapshot(s) exhausted retries")
    if failed_sources:
        need.append(f"source(s) failing: {', '.join(failed_sources)}")
    if not chain.ok:
        need.append(f"evidence chain broken at seq {chain.first_bad_seq}")
    if not runs:
        need.append(f"no runs in the last {hours} hours (coverage gap)")
    return {
        "agent": "brand-evidence",
        "project": project,
        "generated_at": datetime.now(UTC).isoformat(),
        "window_hours": hours,
        "did": {
            "runs": {r.job: r.status for r in runs},
            "posts_captured": posts,
            "archives_confirmed": confirmed,
        },
        "found": {
            "new_mentions": [
                {"source": m.source, "title": m.title, "url": m.url} for m in new_mentions
            ],
            "failed_sources": failed_sources,
            "failed_runs": failed_runs,
            "chain": chain.describe(),
        },
        "need": need,
    }
