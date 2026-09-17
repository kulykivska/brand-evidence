from pathlib import Path

from brand_evidence.core.store import ArtifactStore, sha256_of


def test_put_is_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    a = store.put(b"hello")
    b = store.put(b"hello")
    assert a.sha256 == b.sha256 == sha256_of(b"hello")
    assert a.path == tmp_path / "store" / a.sha256[:2] / a.sha256
    assert not a.already_present and b.already_present
    assert store.verify(a.sha256)
    assert len(list(store.iter_blobs())) == 1


def test_verify_detects_corruption(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    blob = store.put(b"original")
    blob.path.write_bytes(b"tampered")
    assert not store.verify(blob.sha256)
