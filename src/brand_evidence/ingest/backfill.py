"""Import historical posts from CSV.

Columns: platform,url,body,published_at[,external_id,language].
"""

from __future__ import annotations

import csv
from pathlib import Path

from brand_evidence.app import App
from brand_evidence.core.db import session_scope
from brand_evidence.ingest.capture_pipeline import record_history
from brand_evidence.ingest.hook import DuplicatePostError, ingest_post

REQUIRED = {"platform", "url", "body", "published_at"}


def run_backfill(
    app: App, csv_path: Path, *, capture: bool, history: bool = True
) -> dict[str, int]:
    counts = {"imported": 0, "duplicates": 0, "rows": 0, "historical_snapshots": 0}
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
                )
                counts["imported"] += 1
                if history:
                    with session_scope(app.sessions) as session:
                        added = record_history(
                            app, session, "post", result.post_id, row["url"].strip()
                        )
                    counts["historical_snapshots"] += max(added, 0)
            except DuplicatePostError:
                counts["duplicates"] += 1
    return counts
