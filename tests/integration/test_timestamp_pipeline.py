from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from sqlalchemy import select, text
from typer.testing import CliRunner

from brand_evidence.app import App
from brand_evidence.capture.timestamp import TimestampToken
from brand_evidence.core import evidence_log
from brand_evidence.core.db import session_scope
from brand_evidence.export import export_zip
from brand_evidence.ingest.capture_pipeline import timestamp_chain_head
from brand_evidence.ingest.hook import ingest_post
from tests.factories import FakeArchive, FakeCapturer

runner = CliRunner()


class FakeTSA:
    url = "https://tsa.example/tsr"

    def __init__(self) -> None:
        self.stamped: list[bytes] = []

    def stamp(self, data: bytes) -> TimestampToken:
        self.stamped.append(data)
        return TimestampToken(
            tsr=b"TSR:" + hashlib.sha256(data).digest(),
            gen_time="2026-09-15T04:00:00.000000+00:00",
            digest_hex=hashlib.sha256(data).hexdigest(),
            tsa_url=self.url,
            serial="42",
            policy="1.2.3",
        )


def _seed(be: App) -> None:
    be.archive = FakeArchive()
    ingest_post(
        be,
        platform="threads",
        url="https://threads.net/p/1",
        body="Northwind",
        published_at=None,
        capturer=FakeCapturer(),
    )


def test_chain_head_anchor_is_recorded_and_stamps_only_a_hash(be: App) -> None:
    _seed(be)
    be.tsa = FakeTSA()
    with session_scope(be.sessions) as s:
        head = evidence_log.last_entry(s)
        assert head is not None
        row = timestamp_chain_head(be, s)
    assert (
        row is not None and row.kind == "timestamp" and row.captured_at.startswith("2026-09-15T04")
    )
    assert be.tsa.stamped == [f"{head.seq}:{head.entry_hash}".encode()]
    assert be.store.verify(row.sha256)
    with be.sessions() as s:
        events = [e.event_type for e in s.scalars(select(evidence_log.EvidenceEntry))]
        assert events[-1] == "chain.timestamped"
        assert evidence_log.verify(s).ok


def test_empty_chain_has_nothing_to_anchor(be: App) -> None:
    be.tsa = FakeTSA()
    with session_scope(be.sessions) as s:
        assert timestamp_chain_head(be, s) is None


def test_export_ships_manifest_tsr_and_logs_the_export(be: App, tmp_path: Path) -> None:
    _seed(be)
    be.tsa = FakeTSA()
    path = export_zip(be, tmp_path / "e.zip")
    with zipfile.ZipFile(path) as zf:
        manifest = zf.read("MANIFEST.txt")
        assert zf.read("MANIFEST.tsr") == b"TSR:" + hashlib.sha256(manifest).digest()
        assert "TIMESTAMP.txt" not in zf.namelist()
        assert "openssl ts -verify" in zf.read("README.txt").decode()
    assert be.tsa.stamped == [manifest]
    with be.sessions() as s:
        last = evidence_log.last_entry(s)
        assert last is not None and last.event_type == "export.created"
        assert last.payload["manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
        assert last.payload["timestamp"]["serial"] == "42"


def test_export_without_tsa_says_so(be: App, tmp_path: Path) -> None:
    _seed(be)
    be.tsa = None
    with zipfile.ZipFile(export_zip(be, tmp_path / "e.zip")) as zf:
        assert "disabled" in zf.read("TIMESTAMP.txt").decode()
        assert "MANIFEST.tsr" not in zf.namelist()


def test_verify_detects_anchor_that_no_longer_matches(be: App, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _seed(be)
    be.tsa = FakeTSA()
    with session_scope(be.sessions) as s:
        row = timestamp_chain_head(be, s)
        assert row is not None
    # Forge the anchored entry (attacker drops the trigger); the token now points at a lie.
    with be.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER evidence_log_no_update"))
        conn.exec_driver_sql(
            "UPDATE evidence_log SET entry_hash='f'||substr(entry_hash,2) WHERE seq=?",
            (row.capture_meta["stamped_seq"],),
        )
    from brand_evidence.cli import _check_timestamps

    assert len(_check_timestamps(be)) == 1
