"""Daily digest: facts and status only. An empty day renders as an empty day."""

from __future__ import annotations

import smtplib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.clock import parse_iso
from brand_evidence.core.db import session_scope
from brand_evidence.core.models import ArchiveSnapshot, EvidenceEntry, Mention, Post, Run
from brand_evidence.core.runs import tracked_run

TEMPLATES = Path(__file__).with_name("templates")


@dataclass
class DigestOutput:
    date: date
    markdown: str
    path: Path


def build_digest(app: App, for_date: date | None = None, *, email: bool = False) -> DigestOutput:
    for_date = for_date or datetime.now(UTC).date()
    with tracked_run(app.sessions, "digest") as ctx:
        context = _collect(app, for_date)
        env = Environment(
            loader=FileSystemLoader(TEMPLATES),
            autoescape=select_autoescape(default=False),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        markdown = env.get_template("daily.md.j2").render(**context)
        app.settings.digest_path.mkdir(parents=True, exist_ok=True)
        path = app.settings.digest_path / f"{for_date.isoformat()}.md"
        path.write_text(markdown, encoding="utf-8")
        ctx.stats = {k: v for k, v in context["totals"].items()}
        ctx.stats["new_mentions"] = len(context["new_mentions"])
        if email:
            try:
                _send_email(app, for_date, markdown)
                ctx.stats["emailed"] = True
            except Exception as exc:  # noqa: BLE001
                ctx.mark_partial(f"email failed: {exc}")
    return DigestOutput(for_date, markdown, path)


def _collect(app: App, for_date: date) -> dict[str, Any]:
    day_start = datetime(for_date.year, for_date.month, for_date.day, tzinfo=UTC)
    with session_scope(app.sessions) as session:
        last_digest = session.scalars(
            select(Run)
            .where(
                Run.job == "digest", Run.status != "running", Run.started_at < day_start.isoformat()
            )
            .order_by(Run.started_at.desc())
            .limit(1)
        ).first()
        window_start = (
            parse_iso(last_digest.started_at) if last_digest else day_start - timedelta(days=1)
        ).isoformat()

        runs = session.scalars(
            select(Run)
            .where(Run.started_at >= window_start, Run.status != "running")
            .order_by(Run.started_at.asc())
        ).all()
        posts = session.scalars(
            select(Post).where(Post.ingested_at >= window_start).order_by(Post.ingested_at)
        ).all()
        archive_by_subject: dict[str, ArchiveSnapshot] = {}
        for snap in session.scalars(select(ArchiveSnapshot)).all():
            best = archive_by_subject.get(snap.subject_id)
            if best is None or _rank(snap.status) > _rank(best.status):
                archive_by_subject[snap.subject_id] = snap
        new_mentions = session.scalars(
            select(Mention)
            .where(Mention.discovered_at >= window_start)
            .order_by(Mention.discovered_at)
        ).all()
        stale_cutoff = (day_start - timedelta(days=7)).isoformat()
        stale = session.scalars(
            select(Mention).where(Mention.status == "new", Mention.discovered_at < stale_cutoff)
        ).all()
        stuck = _stuck(session)
        chain = evidence_log.verify(session)
        totals = {
            "posts": session.scalar(select(func.count()).select_from(Post)) or 0,
            "mentions": session.scalar(select(func.count()).select_from(Mention)) or 0,
            "evidence_entries": session.scalar(
                select(func.count()).select_from(EvidenceEntry)
            )
            or 0,
            "coverage_days": _coverage_days(session, for_date),
        }
        return {
            "date": for_date.isoformat(),
            "window_start": window_start,
            "runs": [
                {
                    "job": r.job,
                    "status": r.status,
                    "started_at": r.started_at,
                    "failed_sources": (r.stats or {}).get("failed_sources", []),
                    "skipped_sources": (r.stats or {}).get("skipped_sources", []),
                    # Every line: mark_partial appends one per failure, and
                    # printing only the first hid the rest.
                    "errors": (r.error or "").splitlines(),
                    "uncaptured": len((r.stats or {}).get("capture_skipped", []) or []),
                }
                for r in runs
            ],
            "posts": [
                {
                    "platform": p.platform,
                    "url": p.url,
                    "archive": (
                        archive_by_subject[p.id].status if p.id in archive_by_subject else "none"
                    ),
                    "snapshot_url": (
                        archive_by_subject[p.id].snapshot_url
                        if p.id in archive_by_subject
                        else None
                    ),
                }
                for p in posts
            ],
            "new_mentions": [
                {
                    "source": m.source,
                    "title": m.title or "(no title)",
                    "url": m.url,
                    "excerpt": m.excerpt[:300],
                }
                for m in new_mentions
            ],
            "stale_mentions": [
                {"id": m.id, "url": m.url, "discovered_at": m.discovered_at[:10]} for m in stale
            ],
            "stuck_snapshots": [
                {
                    "subject_type": s.subject_type,
                    "url": _target(s),
                    "attempts": s.attempts,
                    "status": s.status,
                }
                for s in stuck
            ],
            "chain": chain.describe(),
            "chain_ok": chain.ok,
            "totals": totals,
        }


def _rank(status: str) -> int:
    """Which snapshot best describes a subject: confirmed, then pending."""
    return {"confirmed": 3, "historical": 2, "pending": 1}.get(status, 0)


def _stuck(session: Session) -> Sequence[ArchiveSnapshot]:
    return session.scalars(select(ArchiveSnapshot).where(ArchiveSnapshot.status == "failed")).all()


def _target(snapshot: ArchiveSnapshot) -> str:
    if "/save/" in snapshot.request_url:
        return unquote(snapshot.request_url.split("/save/", 1)[1])
    return snapshot.request_url


def _coverage_days(session, for_date: date) -> int:  # type: ignore[no-untyped-def]
    """Consecutive days ending yesterday with at least one finished crawl or digest run."""
    days = {
        r.started_at[:10]
        for r in session.scalars(
            select(Run).where(Run.job.in_(("crawl", "digest")), Run.status.in_(("ok", "partial")))
        )
    }
    count = 0
    cursor = for_date - timedelta(days=1)
    while cursor.isoformat() in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def _send_email(app: App, for_date: date, markdown: str) -> None:
    parts = urlsplit(app.settings.smtp_url)
    msg = EmailMessage()
    msg["Subject"] = f"brand-evidence digest {for_date.isoformat()}"
    msg["From"] = app.settings.digest_email_from
    msg["To"] = app.settings.digest_email_to
    msg.set_content(markdown)
    host, port = (
        parts.hostname or "localhost",
        parts.port or (465 if parts.scheme == "smtps" else 587),
    )
    smtp_cls = smtplib.SMTP_SSL if parts.scheme == "smtps" else smtplib.SMTP
    with smtp_cls(host, port, timeout=30) as smtp:
        if parts.scheme == "smtp":
            smtp.starttls()
        if parts.username:
            smtp.login(unquote(parts.username), unquote(parts.password or ""))
        smtp.send_message(msg)
