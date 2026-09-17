"""Evidence package: SQLite dump, artifacts, evidence log CSV, MANIFEST.txt, README.txt."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.clock import now_iso
from brand_evidence.core.db import session_scope
from brand_evidence.core.models import EvidenceEntry

README_TXT = """brand-evidence export
=====================

Generated: {generated}
Chain status at export time: {chain}

Contents
--------
- brand-evidence.sqlite   Full database snapshot (SQLite 3). Open with `sqlite3`.
- evidence_log.csv        The append-only, hash-chained log. Columns:
                          seq, occurred_at, event_type, payload, prev_hash, entry_hash
- store/<aa>/<sha256>     Captured artifacts (PNG, PDF, HTML), named by SHA-256 of content.
- MANIFEST.txt            "<sha256>  <path>" for every file in this package.
- MANIFEST.tsr            RFC 3161 timestamp token over MANIFEST.txt from {tsa_url}
                          (absent when timestamping was disabled or failed; see TIMESTAMP.txt).
- store/*/... kind=timestamp  Daily anchors: timestamp tokens over "<seq>:<entry_hash>" of the
                          chain head, listed in evidence_log as chain.timestamped events.

Verify the package (standard tools only)
----------------------------------------
1. Check every file matches the manifest:
       shasum -a 256 -c MANIFEST.txt
   (or `sha256sum -c MANIFEST.txt` on Linux).

2. Verify the hash chain in evidence_log.csv. For each row in seq order:
       entry_hash == sha256(prev_hash + occurred_at + event_type + payload)
   where payload is the exact JSON string in the CSV (keys sorted, no spaces),
   prev_hash of the first row is 64 zeros, and each row's prev_hash equals the
   previous row's entry_hash. Python one-liner:

       python3 -c "import csv,hashlib;p='0'*64
for r in csv.DictReader(open('evidence_log.csv')):
    h=hashlib.sha256((r['prev_hash']+r['occurred_at']+r['event_type']+r['payload']).encode()).hexdigest()
    assert r['prev_hash']==p and h==r['entry_hash'], r['seq']; p=h
print('chain ok')"

3. Confirm when the manifest existed, using only openssl and the TSA's public certificate chain
   (download it from the TSA's website, e.g. https://freetsa.org/files/cacert.pem):
       openssl ts -reply -in MANIFEST.tsr -text          # shows the TSA's time and the digest
       openssl ts -verify -data MANIFEST.txt -in MANIFEST.tsr -CAfile cacert.pem
   Every file in the package is then proven to have existed at that time, because the
   manifest lists their hashes and the manifest itself is what was stamped.

4. Artifact rows in the log carry a sha256 field; the file store/<first two chars>/<sha256>
   is that exact content. Recompute with `shasum -a 256 store/xx/<sha256>`.

Third-party timestamps
----------------------
archive_snapshots rows hold `snapshot_url` values on web.archive.org. Those snapshots are
held by the Internet Archive, not by the owner of this package.
"""


def export_zip(app: App, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest: list[tuple[str, str]] = []

    def add_bytes(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
        zf.writestr(name, data)
        manifest.append((hashlib.sha256(data).hexdigest(), name))

    with session_scope(app.sessions) as session:
        chain = evidence_log.verify(session).describe()
        entries = session.scalars(select(EvidenceEntry).order_by(EvidenceEntry.seq)).all()
        log_csv = io.StringIO()
        writer = csv.writer(log_csv, lineterminator="\n")
        writer.writerow(["seq", "occurred_at", "event_type", "payload", "prev_hash", "entry_hash"])
        for e in entries:
            writer.writerow(
                [
                    e.seq,
                    e.occurred_at,
                    e.event_type,
                    evidence_log.canonical_json(e.payload),
                    e.prev_hash,
                    e.entry_hash,
                ]
            )

    db_bytes = _dump_sqlite(app.settings.db_path)

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        add_bytes(zf, "brand-evidence.sqlite", db_bytes)
        add_bytes(zf, "evidence_log.csv", log_csv.getvalue().encode("utf-8"))
        for blob_path in app.store.iter_blobs():
            rel = f"store/{blob_path.parent.name}/{blob_path.name}"
            add_bytes(zf, rel, blob_path.read_bytes())
        add_bytes(
            zf,
            "README.txt",
            README_TXT.format(
                generated=now_iso(), chain=chain, tsa_url=app.tsa.url if app.tsa else "(disabled)"
            ).encode(),
        )
        manifest_text = "".join(
            f"{digest}  {name}\n" for digest, name in sorted(manifest, key=lambda x: x[1])
        )
        manifest_bytes = manifest_text.encode("utf-8")
        zf.writestr("MANIFEST.txt", manifest_bytes)
        meta: dict[str, object] = {"generated": now_iso(), "chain": chain}
        if app.tsa is None:
            zf.writestr("TIMESTAMP.txt", "Timestamping disabled (BE_TSA_URL empty).\n")
            meta["timestamp"] = None
        else:
            try:
                token = app.tsa.stamp(manifest_bytes)
                zf.writestr("MANIFEST.tsr", token.tsr)
                meta["timestamp"] = {
                    "tsa_url": token.tsa_url,
                    "gen_time": token.gen_time,
                    "serial": token.serial,
                    "policy": token.policy,
                }
            except Exception as exc:  # noqa: BLE001 - the package is still valid without it
                zf.writestr("TIMESTAMP.txt", f"Timestamp request to {app.tsa.url} failed: {exc}\n")
                meta["timestamp"] = {"error": str(exc)}
        zf.writestr("export_meta.json", json.dumps(meta, indent=2))
    with session_scope(app.sessions) as session:
        evidence_log.append(
            session,
            "export.created",
            {
                "path": str(out),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "files": len(manifest),
                "timestamp": meta["timestamp"],
            },
        )
    return out


def _dump_sqlite(db_path: Path) -> bytes:
    """Consistent snapshot via the online backup API, independent of WAL state."""
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(":memory:")
    try:
        src.backup(dst)
        return b"".join(_serialize(dst))
    finally:
        src.close()
        dst.close()


def _serialize(conn: sqlite3.Connection) -> list[bytes]:
    if hasattr(conn, "serialize"):
        return [conn.serialize()]
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        disk = sqlite3.connect(tmp.name)
        conn.backup(disk)
        disk.close()
        return [Path(tmp.name).read_bytes()]
