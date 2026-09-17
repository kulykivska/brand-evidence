from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from brand_evidence.app import App
from brand_evidence.cli import app as cli
from brand_evidence.core.models import Run
from brand_evidence.digest.builder import build_digest
from brand_evidence.export import export_zip
from brand_evidence.ingest.hook import ingest_post
from tests.factories import FakeArchive, FakeCapturer

runner = CliRunner()


def test_empty_digest_states_zero_plainly(be: App) -> None:
    out = build_digest(be, date(2026, 9, 14))
    assert out.path.name == "2026-09-14.md"
    md = out.markdown
    assert "0 posts captured." in md
    assert "0 new mentions." in md
    assert "chain valid: 0 entries" in md
    assert "| 0 | 0 | 0 | 0 |" in md
    for banned in ("recommend", "suggest", "consider", "should"):
        assert banned not in md.lower()
    with be.sessions() as s:
        assert s.scalars(select(Run).where(Run.job == "digest")).one().status == "ok"


def test_digest_lists_captured_post_and_archive_status(be: App) -> None:
    be.archive = FakeArchive(confirm_on_submit=True)
    ingest_post(
        be,
        platform="linkedin",
        url="https://linkedin.com/posts/rm-1",
        body="Northwind",
        published_at=None,
        capturer=FakeCapturer(),
    )
    md = build_digest(be).markdown
    assert "| linkedin | https://linkedin.com/posts/rm-1 | confirmed" in md
    assert "chain valid: 5 entries" in md


def test_export_is_independently_verifiable(be: App, tmp_path: Path) -> None:
    be.archive = FakeArchive()
    ingest_post(
        be,
        platform="threads",
        url="https://threads.net/p/9",
        body="Northwind",
        published_at=None,
        capturer=FakeCapturer(),
    )
    zip_path = export_zip(be, tmp_path / "out" / "evidence.zip")
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert {"MANIFEST.txt", "README.txt", "evidence_log.csv", "brand-evidence.sqlite"} <= names
        manifest = dict(
            (line.split("  ", 1)[1], line.split("  ", 1)[0])
            for line in zf.read("MANIFEST.txt").decode().splitlines()
        )
        for name, digest in manifest.items():
            assert hashlib.sha256(zf.read(name)).hexdigest() == digest, name
        assert sum(1 for n in names if n.startswith("store/")) == 3
        # Re-verify the chain with only the CSV and the documented formula.
        prev = "0" * 64
        rows = list(csv.DictReader(io.StringIO(zf.read("evidence_log.csv").decode())))
        assert len(rows) == 5
        for r in rows:
            h = hashlib.sha256(
                (r["prev_hash"] + r["occurred_at"] + r["event_type"] + r["payload"]).encode()
            ).hexdigest()
            assert r["prev_hash"] == prev and h == r["entry_hash"], r["seq"]
            prev = h
        db_file = tmp_path / "dump.sqlite"
        db_file.write_bytes(zf.read("brand-evidence.sqlite"))
    conn = sqlite3.connect(db_file)
    assert conn.execute("select count(*) from evidence_log").fetchone()[0] == 5
    conn.close()


def _env(be: App) -> dict[str, str]:
    s = be.settings
    return {
        "BE_DB_PATH": str(s.db_path),
        "BE_STORE_PATH": str(s.store_path),
        "BE_DIGEST_PATH": str(s.digest_path),
        "BE_BRAND_TERMS": ",".join(s.brand_terms),
        "BE_ARCHIVE_PROVIDER": "wayback",
        "BE_WAYBACK_ACCESS_KEY": "test-access",
        "BE_WAYBACK_SECRET_KEY": "test-secret",
        "BE_LOG_LEVEL": "warning",
    }


def test_cli_verify_and_mentions_roundtrip(be: App, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(be.settings.db_path.parent)
    result = runner.invoke(cli, ["verify"], env=_env(be))
    assert result.exit_code == 0, result.output
    assert "chain valid" in result.output
    result = runner.invoke(cli, ["mentions", "list"], env=_env(be))
    assert result.exit_code == 0 and "0 mentions" in result.output
    result = runner.invoke(cli, ["mentions", "set-status", "nope", "reviewed"], env=_env(be))
    assert result.exit_code == 1


def test_cli_fails_fast_on_missing_config(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    for var in (
        "BE_DB_PATH",
        "BE_STORE_PATH",
        "BE_DIGEST_PATH",
        "BE_BRAND_TERMS",
        "BE_ARCHIVE_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(cli, ["verify"], env={"BE_DB_PATH": str(tmp_path / "x.db")})
    assert result.exit_code == 2
    assert "Configuration incomplete" in result.output


def test_schedule_dry_run_names_four_jobs(be: App, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(be.settings.db_path.parent)
    result = runner.invoke(cli, ["schedule", "install", "--dry-run"], env=_env(be))
    assert result.exit_code == 0, result.output
    assert result.output.count("would write") == 4
    assert "timestamp.plist" in result.output
