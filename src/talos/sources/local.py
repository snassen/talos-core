"""Importing message files from disk: .eml, and Apple Mail's .emlx.

Useful for tests, for one-off imports, and for reading an Apple Mail store. An
.emlx file is the message's byte count on the first line, the message itself, then
an Apple property list, which is ignored. The provider key is the SHA-256 of the
message bytes, so importing the same file twice changes nothing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from talos.ingest import Location
from talos.sources.base import SyncContext, SyncStats, chunks

SUFFIXES = (".eml", ".emlx")


def read_message(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix == ".emlx":
        first, _, rest = data.partition(b"\n")
        try:
            return rest[: int(first.strip())]
        except ValueError:
            return rest
    return data


class LocalSource:
    def __init__(self, paths: list[Path], *, folder: str = "local"):
        self.paths = paths
        self.folder = folder

    def files(self) -> list[Path]:
        out = []
        for p in self.paths:
            p = Path(p)
            if p.is_dir():
                out.extend(sorted(f for f in p.rglob("*") if f.suffix in SUFFIXES and f.is_file()))
            elif p.suffix in SUFFIXES:
                out.append(p)
        return out

    def sync(self, ctx: SyncContext) -> SyncStats:
        stats = SyncStats()
        files = self.files()
        if ctx.limit is not None:
            files = files[: ctx.limit]
        for chunk in chunks(files, 200):
            try:
                with ctx.conn.transaction():
                    for f in chunk:
                        raw = read_message(f)
                        stats.seen += 1
                        loc = Location(folder=self.folder, provider_key="sha256:" + hashlib.sha256(raw).hexdigest())
                        res = ctx.ingestor.ingest(ctx.account_id, raw, loc)
                        stats.added += res.created
                        stats.updated += not res.created and not res.failed
                        stats.failed += res.failed
            except Exception:
                ctx.ingestor.reset_caches()
                raise
        return stats
