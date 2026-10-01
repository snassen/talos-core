"""What a run found: catch rate on attacks and false-alarm rate on ordinary text, per source, kind of text and
rule, and the rules set against Jev where both judged the same samples. Counts, rates and ids only: a report
never shows a sample.

"Caught" means a verdict other than clean (block or review: either way no agent reads the raw text); "blocked"
counts block alone. The combination takes the stricter verdict of the two runs.
"""

from __future__ import annotations

from collections import defaultdict

import psycopg

ORDER = {"clean": 0, "review": 1, "block": 2, "error": 0}


def latest(conn: psycopg.Connection, engine: str) -> str | None:
    r = conn.execute("select id from screen_run where engine = %s and finished_at is not null order by started_at desc limit 1",
                     (engine,)).fetchone()
    return r["id"] if r else None


def _rows(conn, run_id: str) -> list[dict]:
    return conn.execute(
        "select r.sample_id, r.verdict, r.fired, s.label, s.source_id, s.kind, coalesce(s.category, '') as category"
        " from screen_result r join screen_sample s on s.id = r.sample_id where r.run_id = %s", (run_id,)).fetchall()


def _rate(n: int, d: int) -> str:
    return f"{n / d:6.1%} ({n}/{d})" if d else "     – (0)"


def tally(rows, key=lambda r: "all") -> dict:
    t: dict = defaultdict(lambda: {"attack": 0, "caught": 0, "blocked": 0, "benign": 0, "alarms": 0})
    for r in rows:
        g = t[key(r)]
        if r["label"] == "attack":
            g["attack"] += 1
            g["caught"] += r["verdict"] in ("review", "block")
            g["blocked"] += r["verdict"] == "block"
        else:
            g["benign"] += 1
            g["alarms"] += r["verdict"] in ("review", "block")
    return dict(t)


def _table(title: str, t: dict) -> list[str]:
    out = [title, f"  {'':28} {'caught':>18} {'blocked':>18} {'false alarms':>18}"]
    for k in sorted(t, key=lambda k: -(t[k]["attack"] + t[k]["benign"])):
        g = t[k]
        out.append(f"  {str(k)[:28]:28} {_rate(g['caught'], g['attack']):>18} {_rate(g['blocked'], g['attack']):>18}"
                   f" {_rate(g['alarms'], g['benign']):>18}")
    return out + [""]


def by_rule(rows) -> dict:
    t: dict = defaultdict(lambda: {"attack": 0, "benign": 0})
    for r in rows:
        for rule in r["fired"]:
            t[rule][r["label"]] += 1
    return dict(t)


def compare(rules_rows, jev_rows) -> dict:
    j = {r["sample_id"]: r for r in jev_rows}
    both = [(r, j[r["sample_id"]]) for r in rules_rows if r["sample_id"] in j and j[r["sample_id"]]["verdict"] != "error"]
    c = defaultdict(int)
    for a, b in both:
        ra, rb = a["verdict"] != "clean", b["verdict"] != "clean"
        side = "attack" if a["label"] == "attack" else "benign"
        c[f"{side}:{'both' if ra and rb else 'rules only' if ra else 'jev only' if rb else 'neither'}"] += 1
    combined = [{**a, "verdict": max(a["verdict"], b["verdict"], key=ORDER.get)} for a, b in both]
    return {"counts": dict(c), "combined": tally(combined)["all"] if combined else None, "samples": len(both)}


def report(conn: psycopg.Connection, run_id: str | None = None, *, against: str | None = None) -> str:
    run_id = run_id or latest(conn, "rules") or latest(conn, "jev")
    if not run_id:
        return "No run yet: talos screen rules (free), or talos screen jev (estimated first)."
    run = conn.execute("select * from screen_run where id = %s", (run_id,)).fetchone()
    rows = _rows(conn, run_id)
    out = [f"Run {run_id} ({run['engine']}, version {run['version']}, {run['samples']:,} samples"
           + (f", ${float(run['cost_usd']):.4f}" if run["engine"] == "jev" else "") + ")", ""]
    out += _table("Overall", tally(rows))
    out += _table("By source", tally(rows, lambda r: r["source_id"]))
    out += _table("By kind of text", tally(rows, lambda r: r["kind"]))
    out += _table("By category (largest first)", dict(sorted(tally(rows, lambda r: f"{r['source_id']}:{r['category']}").items(),
                                                             key=lambda kv: -(kv[1]["attack"] + kv[1]["benign"]))[:15]))
    if run["engine"] == "rules":
        out.append("By rule: fires on attacks / on ordinary text (precision)")
        for rule, n in sorted(by_rule(rows).items(), key=lambda kv: -(kv[1]["attack"] + kv[1]["benign"])):
            tot = n["attack"] + n["benign"]
            out.append(f"  {rule:28} {n['attack']:7,} / {n['benign']:7,}   {n['attack'] / tot:6.1%}")
        out.append("")
    other = against or latest(conn, "jev" if run["engine"] == "rules" else "rules")
    if other and other != run_id:
        r_rows, j_rows = (rows, _rows(conn, other)) if run["engine"] == "rules" else (_rows(conn, other), rows)
        cmp = compare(r_rows, j_rows)
        if cmp["samples"]:
            c = cmp["counts"]
            out += [f"Rules against Jev ({other if run['engine'] == 'rules' else run_id}), on the {cmp['samples']:,} samples both judged",
                    f"  attacks caught by both {c.get('attack:both', 0):,}, rules only {c.get('attack:rules only', 0):,},"
                    f" Jev only {c.get('attack:jev only', 0):,}, neither {c.get('attack:neither', 0):,}",
                    f"  false alarms by both {c.get('benign:both', 0):,}, rules only {c.get('benign:rules only', 0):,},"
                    f" Jev only {c.get('benign:jev only', 0):,}"]
            g = cmp["combined"]
            out += [f"  combined (the stricter verdict): caught {_rate(g['caught'], g['attack'])},"
                    f" false alarms {_rate(g['alarms'], g['benign'])}", ""]
    return "\n".join(out) + "\n"
