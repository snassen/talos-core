"""L0: the vault. Content-addressed, write-once storage for originals.

A blob is named by the SHA-256 of its *uncompressed* bytes, so the name identifies
the content however it is stored. Raw messages are stored zstd-compressed (lossless,
standard library since Python 3.14); attachments are stored as they are, since PDFs,
images and Office files are already compressed.

Writes go to a temporary file, are fsynced, then renamed into place. A blob that
already exists is never rewritten: putting the same bytes twice is a no-op.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from compression import zstd

KINDS = {"raw": ("raw", ".eml.zst", "zstd"), "attachment": ("att", "", "none")}
# A Teams message's original is its Graph JSON, hashed over canonical bytes (talos.teams_ingest.canonical).
KINDS["json"] = ("json", ".json.zst", "zstd")


class BlobUnavailable(OSError):
    """A blob that was written but can no longer be read: removed or locked by security software."""

    def __init__(self, sha: str, kind: str, cause: Exception):
        super().__init__(f"vault {kind} blob {sha[:12]}… is unavailable ({type(cause).__name__}); "
                         f"most likely removed or blocked by the Mac's security software")
        self.sha, self.kind = sha, kind


@dataclass(frozen=True)
class BlobRef:
    sha256: str
    kind: str
    size: int
    stored_size: int
    codec: str
    path: str  # relative to the vault root


class Vault:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _rel(self, kind: str, sha: str) -> str:
        folder, suffix, _ = KINDS[kind]
        return f"{folder}/{sha[:2]}/{sha[2:4]}/{sha}{suffix}"

    def put(self, data: bytes, kind: str) -> BlobRef:
        if kind not in KINDS:
            raise ValueError(f"unknown blob kind {kind!r}")
        sha = hashlib.sha256(data).hexdigest()
        codec = KINDS[kind][2]
        rel = self._rel(kind, sha)
        path = self.root / rel
        if path.exists():
            return BlobRef(sha, kind, len(data), path.stat().st_size, codec, rel)
        stored = zstd.compress(data, level=9) if codec == "zstd" else data
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(stored)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return BlobRef(sha, kind, len(data), len(stored), codec, rel)

    def get(self, sha: str, kind: str) -> bytes:
        path = self.root / self._rel(kind, sha)
        try:
            data = path.read_bytes()
        except (FileNotFoundError, PermissionError) as exc:
            # On this Mac, Microsoft Defender removes or locks files it judges malicious, such
            # as a phishing HTML attachment. That is the security software doing its job; the
            # caller records it rather than failing.
            raise BlobUnavailable(sha, kind, exc) from exc
        if KINDS[kind][2] == "zstd":
            data = zstd.decompress(data)
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"vault blob {sha} does not match its hash")
        return data

    def exists(self, sha: str, kind: str) -> bool:
        return (self.root / self._rel(kind, sha)).exists()
