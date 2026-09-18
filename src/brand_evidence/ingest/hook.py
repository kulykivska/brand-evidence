"""Push path: called at publish time with the platform's own metadata."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from brand_evidence.app import App
from brand_evidence.capture.screenshot import Capturer
from brand_evidence.core import evidence_log
from brand_evidence.core.clock import now_iso
from brand_evidence.core.db import session_scope
from brand_evidence.core.ids import uuid7
from brand_evidence.core.logging import get_logger
from brand_evidence.core.models import Artifact, Post
from brand_evidence.core.runs import tracked_run
from brand_evidence.ingest.capture_pipeline import make_capturer, store_artifacts, submit_archive
from brand_evidence.sources.base import terms_in

log = get_logger(__name__)

PLATFORMS = ("linkedin", "threads", "x", "other")
LANGUAGES = ("en", "uk", "ru")


@dataclass(frozen=True)
class HookResult:
    post_id: str
    created: bool
    artifact_hashes: dict[str, str]
    archive_status: str


class DuplicatePostError(Exception):
    pass


def ingest_post(
    app: App,
    *,
    platform: str,
    url: str,
    body: str,
    published_at: str | None,
    external_id: str | None = None,
    language: str = "en",
    source_path: str = "hook",
    capturer: Capturer | None = None,
    capture: bool = True,
    # Only for a URL the caller wrote themselves, such as a file:// export.
    allow_local: bool = False,
    # A batch caller (backfill) has a run of its own; one run row per CSV line
    # would bury the digest.
    track: bool = True,
) -> HookResult:
    """Record one post. Wrapped in a run so a hook that dies before capture
    leaves a trace in the digest rather than only in a launchd log."""
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {PLATFORMS}")
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {LANGUAGES}")

    def run() -> HookResult:
        return _ingest_post(
            app,
            platform=platform,
            url=url,
            body=body,
            published_at=published_at,
            external_id=external_id,
            language=language,
            source_path=source_path,
            capturer=capturer,
            capture=capture,
            allow_local=allow_local,
        )

    if not track:
        return run()

    duplicate: DuplicatePostError | None = None
    with tracked_run(app.sessions, "hook") as ctx:
        try:
            result = run()
        except DuplicatePostError as exc:
            # An expected outcome, not a failed run: otherwise re-posting a URL
            # files a traceback the operator has to read and dismiss.
            ctx.stats.update({"duplicate": True, "url": url})
            duplicate = exc
        else:
            ctx.stats.update(
                {
                    "post_id": result.post_id,
                    "created": result.created,
                    "archive": result.archive_status,
                    "artifacts": len(result.artifact_hashes),
                }
            )
    if duplicate is not None:
        raise duplicate
    return result


def _ingest_post(
    app: App,
    *,
    platform: str,
    url: str,
    body: str,
    published_at: str | None,
    external_id: str | None,
    language: str,
    source_path: str,
    capturer: Capturer | None,
    capture: bool,
    allow_local: bool,
) -> HookResult:
    resumed: str | None = None
    with session_scope(app.sessions) as session:
        existing = session.scalars(select(Post).where(Post.url == url)).first()
        if existing:
            # The post row and its artifacts are written in two transactions,
            # so a capture that timed out leaves a post with no evidence.
            # Only meaningful when capture was asked for: a post recorded with
            # capture off has no artifacts by design and is simply a duplicate.
            captured = session.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(Artifact.subject_type == "post", Artifact.subject_id == existing.id)
            )
            if captured or not capture:
                raise DuplicatePostError(f"post already recorded: {existing.id}")
            resumed = existing.id
            post_id = existing.id
        else:
            post = Post(
                id=uuid7(),
                platform=platform,
                external_id=external_id,
                url=url,
                body=body,
                language=language,
                published_at=published_at or now_iso(),
                ingested_at=now_iso(),
                brand_mentioned=bool(terms_in(body, app.terms)),
                source_path=source_path,
            )
            evidence_log.record(session, "post.created", post)
            post_id = post.id

    # Outside the transaction above: capture is network-bound, and it opens a
    # writing transaction of its own.
    if resumed:
        log.info("post_capture_resumed", post_id=post_id, url=url)
    result = _capture_for_post(
        app,
        post_id=post_id,
        url=url,
        capturer=capturer,
        capture=capture,
        allow_local=allow_local,
        created=not resumed,
    )
    if not resumed:
        log.info(
            "post_ingested", post_id=post_id, platform=platform, archive=result.archive_status
        )
    return result


def _capture_for_post(
    app: App,
    *,
    post_id: str,
    url: str,
    capturer: Capturer | None,
    capture: bool,
    allow_local: bool,
    created: bool,
) -> HookResult:
    """Capture and archive one post. Safe to call again for a post that has
    none, which is how an interrupted ingest is finished."""
    if not capture:
        return HookResult(post_id, created, {}, "skipped")
    own = capturer is None
    capturer = capturer or make_capturer(app)
    try:
        result = capturer.capture_url(url, allow_local=allow_local)
    finally:
        # A browser this call started is a browser this call must stop.
        if own:
            capturer.close()
    with session_scope(app.sessions) as session:
        rows = store_artifacts(app, session, "post", post_id, result)
        hashes = {r.kind: r.sha256 for r in rows}
        snapshot = submit_archive(app, session, "post", post_id, url)
        return HookResult(post_id, created, hashes, snapshot.status)
