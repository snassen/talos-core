"""Rule candidates for talos-doctor, mined from what Jev caught and the rules missed.

The samples are split by a hash of their id: 70% to mine from, 30% held back to measure on. From the mined part:

1. the target: attacks that the latest rules run let through and the latest Jev run caught;
2. phrasings: word n-grams (2 to 4 words) of the text normalized as the screen reads it (hidden characters gone,
   look-alikes made Latin, lower case), counted once per sample;
3. kept when at least MIN_SUPPORT target samples have it, no ordinary sample of the mined part does, and its catches
   come from two sources or more, none with more than MAX_SOURCE_SHARE of them (else it is one dataset's habit: the
   first mining, with no such limit, found mostly the fixed address and subject of one challenge);
4. chosen greedily: each next candidate is the one that covers the most target samples not yet covered.

Each candidate becomes a regex (its words, any punctuation or spacing between them) and is measured on the held-back
part: attacks it catches, attacks among them the rules miss today, and false alarms on ordinary text. A candidate
whose catches nearly all come from one source is likely that dataset's own habit rather than a general sign, and is
shown with that source's share. The owner decides; accepted candidates export as rules for talos-doctor.

Phrasings are short fragments, the same kind of text talos-doctor's rules file holds; samples are never shown.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict

import psycopg
from psycopg.types.json import Jsonb

from talos.screenlab import report
from talos.screenlab.corpus import normalized

NGRAMS = (2, 3, 4)
MIN_SUPPORT = 15
MAX_CANDIDATES = 40
MAX_SOURCE_SHARE = 0.8     # a phrasing whose catches come 80% or more from one source is that dataset's habit
MAX_CHARS = 4000           # the first 4,000 characters of a sample are mined
HELD = 0.3
WORD = re.compile(r"[a-z0-9åäöéüß']+")


def held_back(sample_id: int) -> bool:
    return int(hashlib.sha256(f"screenlab-split-{sample_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < HELD


def lines(text: str) -> list[str]:
    """The text's lines as the screen reads them: talos-doctor judges line by line, so a phrasing must fit in one."""
    return [n for n in (normalized(x) for x in text[:MAX_CHARS].splitlines()) if n]


def grams(text: str) -> set[tuple[str, ...]]:
    out = set()
    for ln in lines(text):
        words = WORD.findall(ln)
        out |= {tuple(words[i:i + n]) for n in NGRAMS for i in range(len(words) - n + 1)}
    return out


def hit(rx: re.Pattern, text: str) -> bool:
    return any(rx.search(ln) for ln in lines(text))


def regex(words: tuple[str, ...]) -> str:
    return r"\b" + r"[\W_]+".join(re.escape(w) for w in words) + r"\b"


def _samples(conn, rules_run: str, jev_run: str) -> list[dict]:
    return conn.execute(
        "select s.id, s.label, s.source_id, s.text, rr.verdict as rules, jr.verdict as jev"
        " from screen_sample s join screen_result rr on rr.sample_id = s.id and rr.run_id = %s"
        " join screen_result jr on jr.sample_id = s.id and jr.run_id = %s where s.dup_of is null",
        (rules_run, jev_run)).fetchall()


def mine(conn: psycopg.Connection, *, rules_run: str | None = None, jev_run: str | None = None,
         min_support: int = MIN_SUPPORT, max_candidates: int = MAX_CANDIDATES,
         max_share: float = MAX_SOURCE_SHARE) -> dict:
    rules_run = rules_run or report.latest(conn, "rules")
    jev_run = jev_run or report.latest(conn, "jev")
    if not rules_run or not jev_run:
        raise ValueError("mining needs a rules run and a Jev run over the same samples")
    rows = _samples(conn, rules_run, jev_run)
    train = [r for r in rows if not held_back(r["id"])]
    held = [r for r in rows if held_back(r["id"])]
    target = [r for r in train if r["label"] == "attack" and r["rules"] == "clean" and r["jev"] not in ("clean", "error")]
    benign_grams: set = set()
    for r in train:
        if r["label"] == "benign":
            benign_grams |= grams(r["text"])
    df: Counter = Counter()
    covers: dict = defaultdict(set)
    for r in target:
        for g in grams(r["text"]) - benign_grams:
            df[g] += 1
            covers[g].add(r["id"])
    source_of = {r["id"]: r["source_id"] for r in target}

    def spread(g) -> bool:
        c = Counter(source_of[i] for i in covers[g])
        return len(c) >= 2 and c.most_common(1)[0][1] / sum(c.values()) <= max_share

    pool = {g for g, n in df.items() if n >= min_support and spread(g)}
    # a longer phrasing that covers the same samples as a shorter one inside it adds nothing
    chosen, covered = [], set()
    while pool and len(chosen) < max_candidates:
        best = max(pool, key=lambda g: (len(covers[g] - covered), -len(g)))
        gain = covers[best] - covered
        if len(gain) < min_support:
            break
        chosen.append(best)
        covered |= gain
        pool.discard(best)
    # measure on the held-back part, and on all ordinary text
    benign_all = [r for r in rows if r["label"] == "benign"]
    held_attacks = [r for r in held if r["label"] == "attack"]
    held_benign = [r for r in held if r["label"] == "benign"]
    run = {"rules_run": rules_run, "jev_run": jev_run, "held_share": HELD, "min_support": min_support, "max_share": max_share,
           "target": len(target), "train": len(train), "held": len(held)}
    conn.execute("delete from screen_candidate where status = 'proposed'")
    made = []
    for g in chosen:
        rx = re.compile(regex(g))
        hits = [r for r in held_attacks if hit(rx, r["text"])]
        src = Counter(r["source_id"] for r in hits) or Counter(r["source_id"] for r in target if r["id"] in covers[g])
        top, n_top = src.most_common(1)[0] if src else (None, 0)
        cand = {"pattern": regex(g), "words": list(g), "train_support": len(covers[g]), "held_attacks": len(hits),
                "held_new": sum(1 for r in hits if r["rules"] == "clean"),
                "held_benign": sum(1 for r in held_benign if hit(rx, r["text"])),
                "benign_all": sum(1 for r in benign_all if hit(rx, r["text"])),
                "top_source": top, "top_share": round(n_top / max(1, sum(src.values())), 3)}
        conn.execute("insert into screen_candidate (pattern, words, train_support, held_attacks, held_new, held_benign,"
                     " benign_all, top_source, top_share, mined_from) values (%(pattern)s, %(words)s, %(train_support)s,"
                     " %(held_attacks)s, %(held_new)s, %(held_benign)s, %(benign_all)s, %(top_source)s, %(top_share)s,"
                     " %(mined_from)s) on conflict (pattern) do nothing", {**cand, "mined_from": Jsonb(run)})
        made.append(cand)
    # what all candidates together would add, on the held-back part
    rxs = [re.compile(c["pattern"]) for c in made]
    newly = [r for r in held_attacks if r["rules"] == "clean" and any(hit(x, r["text"]) for x in rxs)]
    alarms = [r for r in held_benign if any(hit(x, r["text"]) for x in rxs)]
    run.update(candidates=len(made), covered_in_train=len(covered),
               held_attacks=len(held_attacks), held_missed_by_rules=sum(1 for r in held_attacks if r["rules"] == "clean"),
               held_newly_caught=len(newly), held_benign=len(held_benign), held_new_alarms=len(alarms))
    return run


def export(conn: psycopg.Connection, *, severity: str = "flag") -> dict:
    """The accepted candidates as one talos-doctor rule per candidate (rules.json entries), with their measurements."""
    rows = conn.execute("select * from screen_candidate where status = 'accepted' order by id").fetchall()
    rules = []
    for r in rows:
        rules.append({"id": f"mined.{'-'.join(r['words'])[:40]}", "version": 1, "kind": "regex", "severity": severity,
                      "applies": "all", "ignore_case": True, "pattern": r["pattern"],
                      "why": f"Mined in the lab: in {r['held_attacks']} held-back attacks and no ordinary text"
                             f" ({r['held_benign']} false alarms held back, {r['benign_all']} in all)."})
    return {"rules": rules}
