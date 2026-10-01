"""One shape for every sample, and deduplication across all sources.

Exact duplicates share text_key: the sha256 of the text normalized the way talos-doctor's screen reads it
(hidden characters removed, compatibility forms folded, look-alike letters made Latin), lowercased, with
whitespace collapsed. Near duplicates share a MinHash band of their word 5-shingles: BANDS bands of ROWS rows,
and two samples sharing MIN_BANDS bands or more are near duplicates (their shingles overlap about 80% or more). A duplicate keeps a
link to the sample it repeats (dup_of), so each source's own counts stay honest and nothing counts twice.
"""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass

import psycopg

BANDS, ROWS, SHINGLE = 16, 4, 5
MIN_BANDS = 3       # shared bands that make a near duplicate: ~97% of pairs 80% alike, under 10% of pairs 50% alike
_HIDDEN = re.compile("[​-‏⁠-⁤᠎︀-️‪-‮⁦-⁩­﻿"
                     "\U000e0000-\U000e007f\U000e0100-\U000e01ef]")
_CONFUSABLE = str.maketrans("аеорсухіјѕԁԛԝАВЕКМНОРСТХІЈЅοаνρτχκιΑΒΕΖΗΙΚΜΝΟΡΤΥΧ",
                            "aeopcyxijsdqwABEKMHOPCTXIJSoavptxkiABEZHIKMNOPTYX")
_SEEDS = [hashlib.sha256(f"talos-screenlab-{i}".encode()).digest()[:8] for i in range(BANDS * ROWS)]


@dataclass
class Sample:
    ext_id: str
    label: str              # attack or benign
    text: str
    category: str | None = None
    kind: str = "text"
    path_hint: str | None = None


def normalized(text: str) -> str:
    t = unicodedata.normalize("NFKC", _HIDDEN.sub("", text)).translate(_CONFUSABLE).lower()
    return re.sub(r"\s+", " ", t).strip()


def text_key(text: str) -> str:
    return hashlib.sha256(normalized(text).encode()).hexdigest()


def _shingles(norm: str) -> set[bytes]:
    words = norm.split(" ")
    if len(words) < SHINGLE:
        return {norm.encode()}
    return {" ".join(words[i:i + SHINGLE]).encode() for i in range(len(words) - SHINGLE + 1)}


def minhash(norm: str) -> list[int]:
    sh = _shingles(norm)
    return [min(int.from_bytes(hashlib.blake2b(s, digest_size=8, key=seed).digest(), "big") for s in sh)
            for seed in _SEEDS]


def bands(norm: str) -> list[int]:
    """One signed 64-bit number per band (PostgreSQL's bigint), from its ROWS MinHash values and its index."""
    mh = minhash(norm)
    out = []
    for b in range(BANDS):
        d = hashlib.blake2b(struct.pack(">I", b) + b"".join(v.to_bytes(8, "big") for v in mh[b * ROWS:(b + 1) * ROWS]),
                            digest_size=8).digest()
        out.append(int.from_bytes(d, "big", signed=True))
    return out


class Deduper:
    """What is already stored, held in memory while a source imports: exact keys and band buckets."""

    def __init__(self, conn: psycopg.Connection):
        self.keys: dict[str, int] = {}
        self.buckets: dict[int, int] = {}
        for r in conn.execute("select id, text_key, bands from screen_sample where dup_of is null order by id"):
            self.keys.setdefault(r["text_key"], r["id"])
            for b in r["bands"]:
                self.buckets.setdefault(b, r["id"])

    def find(self, key: str, bs: list[int]) -> int | None:
        """The sample this one repeats: the same normalized text, or MIN_BANDS or more shared bands."""
        if key in self.keys:
            return self.keys[key]
        hits = [self.buckets[b] for b in bs if b in self.buckets]
        best = max(set(hits), key=hits.count) if hits else None
        return best if best is not None and hits.count(best) >= MIN_BANDS else None

    def add(self, sid: int, key: str, bs: list[int]) -> None:
        self.keys.setdefault(key, sid)
        for b in bs:
            self.buckets.setdefault(b, sid)


def store(conn: psycopg.Connection, source_id: str, samples, dedup: Deduper | None = None) -> dict:
    """Insert a source's samples (replacing its earlier import). Returns counts; prints nothing of any sample."""
    conn.execute("delete from screen_result where sample_id in (select id from screen_sample where source_id = %s)",
                 (source_id,))
    conn.execute("update screen_sample set dup_of = null where dup_of in (select id from screen_sample where source_id = %s)",
                 (source_id,))
    conn.execute("delete from screen_sample where source_id = %s", (source_id,))
    dedup = dedup or Deduper(conn)
    counts = {"imported": 0, "attack": 0, "benign": 0, "duplicates": 0, "skipped": 0}
    seen: set[str] = set()
    for s in samples:
        if not s.text or not s.text.strip() or s.label not in ("attack", "benign") or s.ext_id in seen:
            counts["skipped"] += 1
            continue
        seen.add(s.ext_id)
        norm = normalized(s.text)
        key, bs = hashlib.sha256(norm.encode()).hexdigest(), bands(norm)
        dup = dedup.find(key, bs)
        sid = conn.execute(
            "insert into screen_sample (source_id, ext_id, label, category, kind, path_hint, text, text_key, bands, dup_of)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
            (source_id, s.ext_id, s.label, s.category, s.kind, s.path_hint, s.text.replace("\x00", ""), key, bs, dup)).fetchone()["id"]
        counts["imported"] += 1
        counts[s.label] += 1
        if dup:
            counts["duplicates"] += 1
        else:
            dedup.add(sid, key, bs)
    return counts
