"""Import historical posts from CSV.

Columns: platform,url,body,published_at[,external_id,language].
"""

from __future__ import annotations

import csv
from contextlib import ExitStack
from pathlib import Path

from brand_evidence.app import App
from brand_evidence.capture.screenshot import Capturer
from brand_evidence.core.db import session_scope
from brand_evidence.core.runs import RunContext, tracked_run
from brand_evidence.ingest.capture_pipeline import make_capturer, record_history
from brand_evidence.ingest.hook import DuplicatePostError, ingest_post

REQUIRED = {"platform", "url", "body", "published_at"}


def run_backfill(
    app: App, csv_path: Path, *, capture: bool, history: bool = True
) -> dict[str, int]:
    with tracked_run(app.sessions, "backfill") as ctx, ExitStack() as closing:
        # One browser for the whole file, not one per row.
        capturer = None
        if capture:
            capturer = make_capturer(app)
            closing.callback(capturer.close)
        # The rows count straight into ctx.stats, so a row that raises halfway
        # still leaves the partial counts on the run row.
        counts: dict[str, int] = ctx.stats
        counts.update({"imported": 0, "duplicates": 0, "rows": 0, "historical_snapshots": 0})
        _backfill_rows(
            app, ctx, csv_path, counts, capture=capture, history=history, capturer=capturer
        )
        return counts


def _backfill_rows(
    app: App,
    ctx: RunContext,
    csv_path: Path,
    counts: dict[str, int],
    *,
    capture: bool,
    history: bool,
    capturer: Capturer | None,
) -> None:
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing columns: {sorted(missing)}")
        for row in reader:
            counts["rows"] += 1
            try:
                result = ingest_post(
                    app,
                    platform=row["platform"].strip(),
                    url=row["url"].strip(),
                    body=row["body"],
                    published_at=row["published_at"].strip() or None,
                    external_id=(row.get("external_id") or "").strip() or None,
                    language=(row.get("language") or "en").strip(),
                    source_path="backfill",
                    capture=capture,
                    capturer=capturer,
                    track=False,
                )
                counts["imported"] += 1
                if history:
                    with session_scope(app.sessions) as session:
                        added = record_history(
                            app, session, "post", result.post_id, row["url"].strip()
                        )
                    if added < 0:
                        # -1 means the lookup never happened, which is not
                        # the same as the archive holding nothing.
                        counts["history_lookups_failed"] = (
                            counts.get("history_lookups_failed", 0) + 1
                        )
                        ctx.mark_partial(f"archive history lookup failed for {row['url']}")
                    else:
                        counts["historical_snapshots"] += added
            except DuplicatePostError:
                counts["duplicates"] += 1
