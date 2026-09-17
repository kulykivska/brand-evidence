"""Pulls the owner's own publications from a JSON feed and records them as posts.

Used when the publisher runs on a server and cannot call the hook on this machine.
The feed is the owner's own system, so the post metadata is still the platform's.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import func, select

from brand_evidence.app import App
from brand_evidence.capture.screenshot import Capturer
from brand_evidence.core.clock import parse_iso
from brand_evidence.core.logging import get_logger
from brand_evidence.core.models import Post
from brand_evidence.core.runs import RunContext
from brand_evidence.ingest.hook import PLATFORMS, DuplicatePostError, ingest_post
from brand_evidence.sources.base import user_agent

log = get_logger(__name__)
SOURCE_PATH = "feed"


def _auth_headers(app: App) -> dict[str, str]:
    s = app.settings
    if not s.own_publications_token:
        return {}
    if s.own_publications_auth == "bearer":
        return {"Authorization": f"Bearer {s.own_publications_token}"}
    return {"X-API-Key": s.own_publications_token}


def _last_seen(app: App) -> str | None:
    with app.sessions() as session:
        return session.scalar(
            select(func.max(Post.published_at)).where(Post.source_path == SOURCE_PATH)
        )


def fetch_feed(app: App, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    params: dict[str, str] = {}
    since = _last_seen(app)
    if since:
        params["since"] = parse_iso(since).isoformat()
    own = client is None
    client = client or httpx.Client(timeout=30, headers={"User-Agent": user_agent()})
    try:
        response = client.get(
            app.settings.own_publications_url, params=params, headers=_auth_headers(app)
        )
        response.raise_for_status()
        data = response.json()
    finally:
        if own:
            client.close()
    items = data.get("items", data) if isinstance(data, dict) else data
    return list(items or [])


def sync_publications(
    app: App,
    ctx: RunContext,
    *,
    capturer: Capturer | None = None,
    capture: bool = True,
    client: httpx.Client | None = None,
) -> int:
    """Record every new publication with a public URL. Returns the number of new posts."""
    try:
        items = fetch_feed(app, client)
    except (httpx.HTTPError, ValueError) as exc:
        # The feed URL can carry a token in its query string, and this text
        # is stored as runs.error and shipped inside export.
        detail = type(exc).__name__
        ctx.mark_partial(f"own publications feed unreachable: {detail}")
        ctx.stats["own_publications"] = {"error": detail}
        return 0
    created = skipped = 0
    for item in items:
        url = item.get("external_url")
        if not url:
            continue
        platform = str(item.get("platform") or "other")
        if platform not in PLATFORMS:
            platform = "other"
        try:
            ingest_post(
                app,
                platform=platform,
                url=url,
                body=str(item.get("text") or ""),
                published_at=item.get("published_at"),
                external_id=item.get("external_id"),
                source_path=SOURCE_PATH,
                capturer=capturer,
                capture=capture,
            )
            created += 1
        except DuplicatePostError:
            skipped += 1
        except Exception as exc:  # noqa: BLE001 - one bad item must not sink the sync
            ctx.mark_partial(f"publication {url} failed: {exc}")
    ctx.stats["own_publications"] = {"fetched": len(items), "new": created, "known": skipped}
    log.info("publications_synced", fetched=len(items), new=created)
    return created
