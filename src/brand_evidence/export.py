"""Evidence package: SQLite dump, artifacts, evidence log CSV, MANIFEST.txt, README.txt."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.clock import now_iso
from brand_evidence.core.db import session_scope
from brand_evidence.core.logging import get_logger
from brand_evidence.core.models import EvidenceEntry

log = get_logger(__name__)

README_TXT = """brand-evidence export
=====================

Generated: {generated}
Chain status at export time: {chain}

Contents
--------
- brand-evidence.sqlite   Full database snapshot (SQLite 3). Open with `sqlite3`.
- evidence_log.csv        The append-only, hash-chained log. Columns:
                          seq, occurred_at, event_type, payload, prev_hash, entry_hash
- export_meta.json        When this package was generated and the chain status then. The
                          timestamp over it is described in TIMESTAMP.txt, because a stamp
                          of the manifest cannot be inside the manifest.
- store/<aa>/<sha256>     Captured artifacts (PNG, PDF, HTML), named by SHA-256 of content.
- MANIFEST.txt            "<sha256>  <path>" for every other file in this package. Only
                          MANIFEST.txt itself, MANIFEST.tsr and TIMESTAMP.txt are absent:
                          the first cannot list its own hash, and the other two describe
                          the stamp taken over it.
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


# Everything is streamed through this, so neither the database nor a 20 GB
# store is ever held in memory.
CHUNK = 1024 * 1024


def _add_stream(
    zf: zipfile.ZipFile, name: str, path: Path, manifest: list[tuple[str, str]]
) -> None:
    """Copy a file into the zip a chunk at a time, hashing the same chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as src, zf.open(name, "w") as dst:
        while chunk := src.read(CHUNK):
            digest.update(chunk)
            dst.write(chunk)
    manifest.append((digest.hexdigest(), name))


def _add_bytes(
    zf: zipfile.ZipFile, name: str, data: bytes, manifest: list[tuple[str, str]]
) -> None:
    zf.writestr(name, data)
    manifest.append((hashlib.sha256(data).hexdigest(), name))


def _write_log_csv(app: App, path: Path) -> str:
    """Stream the evidence log to disk and return the chain description.

    A read-only session: holding the write lock for a full-chain walk and a
    multi-gigabyte CSV would block every other job for the whole export.
    """
    with app.sessions() as session:
        chain = evidence_log.verify(session).describe()
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(
                ["seq", "occurred_at", "event_type", "payload", "prev_hash", "entry_hash"]
            )
            # yield_per: the log only grows, and an export must not need it all
            # resident at once.
            for e in session.scalars(
                select(EvidenceEntry).order_by(EvidenceEntry.seq).execution_options(yield_per=500)
            ):
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
    return chain


def export_zip(app: App, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest: list[tuple[str, str]] = []

    with tempfile.TemporaryDirectory() as workdir:
        work = Path(workdir)
        log_csv = work / "evidence_log.csv"
        chain = _write_log_csv(app, log_csv)
        if not chain.startswith("chain valid"):
            # The package is still produced - it is the evidence of the break -
            # but nobody should have to read the README to learn of it.
            log.error("export_chain_broken", chain=chain, out=str(out))
        db_copy = work / "brand-evidence.sqlite"
        _dump_sqlite(app.settings.db_path, db_copy)

        # Built beside the destination and moved into place: a failure halfway
        # used to leave a truncated zip where the last good package was.
        # Beside it, not in the temp dir, so the move is a rename.
        partial = out.with_name(out.name + ".partial")
        generated = now_iso()
        try:
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                _add_stream(zf, "brand-evidence.sqlite", db_copy, manifest)
                _add_stream(zf, "evidence_log.csv", log_csv, manifest)
                for blob_path in app.store.iter_blobs():
                    rel = f"store/{blob_path.parent.name}/{blob_path.name}"
                    _add_stream(zf, rel, blob_path, manifest)
                _add_bytes(
                    zf,
                    "README.txt",
                    README_TXT.format(
                        generated=generated,
                        chain=chain,
                        tsa_url=app.tsa.url if app.tsa else "(disabled)",
                    ).encode(),
                    manifest,
                )

                # Manifested, so it is written before the manifest and never
                # rewritten afterwards. The timestamp is a stamp OF the manifest, so
                # it cannot be inside it and lives in TIMESTAMP.txt instead.
                _add_bytes(
                    zf,
                    "export_meta.json",
                    json.dumps({"generated": generated, "chain": chain}, indent=2).encode(),
                    manifest,
                )

                manifest_bytes = "".join(
                    f"{digest}  {name}\n" for digest, name in sorted(manifest, key=lambda x: x[1])
                ).encode("utf-8")
                zf.writestr("MANIFEST.txt", manifest_bytes)

                timestamp: dict[str, object] | None = None
                if app.tsa is None:
                    zf.writestr("TIMESTAMP.txt", "Timestamping disabled (BE_TSA_URL empty).\n")
                else:
                    try:
                        token = app.tsa.stamp(manifest_bytes)
                        zf.writestr("MANIFEST.tsr", token.tsr)
                        timestamp = {
                            "tsa_url": token.tsa_url,
                            "gen_time": token.gen_time,
                            "serial": token.serial,
                            "policy": token.policy,
                        }
                        zf.writestr("TIMESTAMP.txt", json.dumps(timestamp, indent=2) + "\n")
                    except Exception as exc:  # noqa: BLE001 - still a valid package
                        zf.writestr(
                            "TIMESTAMP.txt", f"Timestamp request to {app.tsa.url} failed: {exc}\n"
                        )
                        timestamp = {"error": str(exc)}

            partial.replace(out)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    with session_scope(app.sessions) as session:
        evidence_log.append(
            session,
            "export.created",
            {
                "path": str(out),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "files": len(manifest),
                "timestamp": timestamp,
            },
        )
    return out


def _dump_sqlite(db_path: Path, dest: Path) -> None:
    """Consistent snapshot via the online backup API, independent of WAL state.

    Written straight to a file: backing up into an :memory: connection and
    serialising it needed roughly twice the database in RAM.
    """
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
