"""The manifest covers what the README says it covers.

The export exists to be handed to someone else, and its README tells them to
run `shasum -a 256 -c MANIFEST.txt`. Four files used to be written after the
manifest and were therefore absent from it, so that check verified less than
the instructions claimed.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from brand_evidence.app import App
from brand_evidence.export import export_zip
from brand_evidence.ingest.hook import ingest_post
from tests.factories import FakeArchive, FakeCapturer, FakeTSA

# A stamp is taken over the manifest, so these cannot be inside it.
NOT_MANIFESTED = {"MANIFEST.txt", "MANIFEST.tsr", "TIMESTAMP.txt"}


def seed(be: App) -> None:
    be.archive = FakeArchive()
    ingest_post(
        be,
        platform="x",
        url="https://x.com/northwind/status/1",
        body="Northwind ships a thing",
        published_at=None,
        capturer=FakeCapturer(),
    )


def test_every_file_but_the_stamp_is_in_the_manifest(be: App, tmp_path: Path) -> None:
    seed(be)
    be.tsa = FakeTSA()
    with zipfile.ZipFile(export_zip(be, tmp_path / "e.zip")) as zf:
        listed = {
            line.split("  ", 1)[1]
            for line in zf.read("MANIFEST.txt").decode().splitlines()
            if line.strip()
        }
        assert set(zf.namelist()) - listed == NOT_MANIFESTED
        for name in listed:
            assert hashlib.sha256(zf.read(name)).hexdigest() == next(
                line.split("  ", 1)[0]
                for line in zf.read("MANIFEST.txt").decode().splitlines()
                if line.endswith(f"  {name}")
            ), name


def test_export_meta_is_not_rewritten_after_being_manifested(be: App, tmp_path: Path) -> None:
    """It was, which silently broke the manifest for that one file."""
    seed(be)
    be.tsa = FakeTSA()
    with zipfile.ZipFile(export_zip(be, tmp_path / "e.zip")) as zf:
        digest = next(
            line.split("  ", 1)[0]
            for line in zf.read("MANIFEST.txt").decode().splitlines()
            if line.endswith("  export_meta.json")
        )
        assert hashlib.sha256(zf.read("export_meta.json")).hexdigest() == digest


def test_a_failed_export_leaves_the_previous_package_alone(
    be: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zip used to be written straight onto the destination, so a store
    read that threw halfway replaced the last good package with a truncated one."""
    seed(be)
    out = tmp_path / "e.zip"
    export_zip(be, out)
    good = out.read_bytes()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("store went away")

    monkeypatch.setattr(be.store, "iter_blobs", boom)
    with pytest.raises(OSError, match="store went away"):
        export_zip(be, out)
    assert out.read_bytes() == good
    assert not out.with_name(out.name + ".partial").exists()
