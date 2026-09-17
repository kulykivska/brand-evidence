"""Push path: called at publish time with the platform's own metadata."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.capture.screenshot import Capturer
from brand_evidence.core import evidence_log
from brand_evidence.core.clock import now_iso
from brand_evidence.core.db import session_scope
from brand_evidence.core.ids import uuid7
from brand_evidence.core.logging import get_logger
from brand_evidence.core.models import Post
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
) -> HookResult:
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {PLATFORMS}")
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {LANGUAGES}")

    with session_scope(app.sessions) as session:
        existing = session.scalars(select(Post).where(Post.url == url)).first()
        if existing:
            raise DuplicatePostError(f"post already recorded: {existing.id}")
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

    hashes: dict[str, str] = {}
    archive_status = "skipped"
    if capture:
        result = (capturer or make_capturer(app)).capture_url(url, allow_local=allow_local)
        with session_scope(app.sessions) as session:
            rows = store_artifacts(app, session, "post", post_id, result)
            hashes = {r.kind: r.sha256 for r in rows}
            snapshot = submit_archive(app, session, "post", post_id, url)
            archive_status = snapshot.status
    log.info("post_ingested", post_id=post_id, platform=platform, archive=archive_status)
    return HookResult(post_id, True, hashes, archive_status)
