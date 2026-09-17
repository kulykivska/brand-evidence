"""Confirms pending archive snapshots with exponential backoff; gives up after MAX_ATTEMPTS."""

from __future__ import annotations

from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.capture.archiver import MAX_ATTEMPTS, RateLimiter, next_attempt_due
from brand_evidence.core import evidence_log
from brand_evidence.core.clock import now_iso
from brand_evidence.core.db import session_scope
from brand_evidence.core.logging import get_logger
from brand_evidence.core.models import ArchiveSnapshot
from brand_evidence.core.runs import tracked_run

log = get_logger(__name__)


def _target_url(snapshot: ArchiveSnapshot) -> str:
    marker = "/save/"
    if marker in snapshot.request_url:
        return snapshot.request_url.split(marker, 1)[1]
    return snapshot.request_url


def run_archive_poll(app: App) -> str:
    with tracked_run(app.sessions, "archive_poll") as ctx:
        limiter = RateLimiter(app.settings.archive_min_interval_seconds)
        confirmed = failed = deferred = 0
        with session_scope(app.sessions) as session:
            pending = session.scalars(
                select(ArchiveSnapshot).where(ArchiveSnapshot.status == "pending")
            ).all()
            now = now_iso()
            for snap in pending:
                if next_attempt_due(snap.submitted_at, snap.attempts) > now:
                    deferred += 1
                    continue
                limiter.wait()
                snap.attempts += 1
                try:
                    url = app.archive.check(_target_url(snap), snap.submitted_at, snap.provider_ref)
                    if url is None and snap.attempts < MAX_ATTEMPTS and not snap.provider_ref:
                        # No job to wait on: ask the archive again and keep the new job id.
                        resubmitted = app.archive.submit(_target_url(snap))
                        snap.provider_ref = resubmitted.provider_ref
                        url = resubmitted.snapshot_url
                except Exception as exc:  # noqa: BLE001
                    ctx.mark_partial(f"archive check failed for {snap.id}: {exc}")
                    url = None
                if url:
                    snap.snapshot_url = url
                    snap.status = "confirmed"
                    snap.confirmed_at = now_iso()
                    confirmed += 1
                    evidence_log.append(session, "archive_snapshot.confirmed", snap.as_payload())
                elif snap.attempts >= MAX_ATTEMPTS:
                    snap.status = "failed"
                    failed += 1
                    evidence_log.append(session, "archive_snapshot.failed", snap.as_payload())
                else:
                    evidence_log.append(
                        session,
                        "archive_snapshot.attempted",
                        {"id": snap.id, "attempts": snap.attempts},
                    )
        ctx.stats.update(
            {
                "pending_seen": len(pending),
                "confirmed": confirmed,
                "failed": failed,
                "deferred": deferred,
            }
        )
        return ctx.run_id
