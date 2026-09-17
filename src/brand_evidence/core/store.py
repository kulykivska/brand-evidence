"""Content-addressed artifact store: store/{sha256[:2]}/{sha256}. Files are never mutated."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    size: int
    path: Path
    already_present: bool


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def put(self, data: bytes) -> StoredBlob:
        """Hash first, then write atomically. An existing file is left untouched."""
        digest = sha256_of(data)
        target = self.path_for(digest)
        if target.exists():
            return StoredBlob(digest, target.stat().st_size, target, already_present=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        return StoredBlob(digest, len(data), target, already_present=False)

    def get(self, digest: str) -> bytes:
        return self.path_for(digest).read_bytes()

    def verify(self, digest: str) -> bool:
        target = self.path_for(digest)
        return target.exists() and sha256_of(target.read_bytes()) == digest

    def iter_blobs(self) -> Iterator[Path]:
        for shard in sorted(p for p in self.root.iterdir() if p.is_dir()):
            yield from sorted(p for p in shard.iterdir() if p.is_file())
