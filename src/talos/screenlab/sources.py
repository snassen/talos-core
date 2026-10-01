"""The sources, one module each: where its samples come from, its licence, and how they become Samples.

Each source is switched on or off and has a sample cap (`talos screen source ID --on --cap N`); a capped source
takes a stratified sample, the same one every time (it is ordered by a hash, not at random), so a run can be
repeated. Downloads go to TALOS_HOME/screenlab/cache/<source>/, are kept, and their revision (a dataset's
commit, or the file's sha256) is recorded with the import. A gated dataset needs a Hugging Face token in the
Keychain (`huggingface-token`, read by name like every secret).

Nothing here prints a sample. Labels follow each source's own: an attack is text written to steer a model;
benign is ordinary text of the same kind (the sources' controls and false-positive sets, and Talos's own code).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from talos import personal, secrets
from talos.screenlab.corpus import Sample

HF = "https://huggingface.co"
GH_RAW = "https://raw.githubusercontent.com"


class SourceError(RuntimeError):
    pass


@dataclass
class Source:
    id: str
    title: str
    license: str
    url: str
    default_cap: int | None
    load: Callable[["Fetcher"], Iterator[Sample]]
    notes: str = ""


class Fetcher:
    """Downloads into the cache, once, and remembers what it fetched (the revision of the import)."""

    def __init__(self, cache: Path, source_id: str, http: httpx.Client | None = None):
        self.dir = cache / source_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.http = http or httpx.Client(timeout=120, follow_redirects=True)
        self.revisions: list[str] = []

    def hf_revision(self, dataset: str) -> str:
        r = self.http.get(f"{HF}/api/datasets/{dataset}", headers=self._hf_auth())
        r.raise_for_status()
        meta = r.json()
        if meta.get("gated") and not self._hf_auth():
            raise SourceError(f"{dataset} is gated: accept its terms on Hugging Face, then store a token: "
                              "security add-generic-password -U -s talos -a huggingface-token -w")
        return meta["sha"]

    def _hf_auth(self) -> dict:
        token = secrets.get_optional("huggingface-token")
        return {"Authorization": f"Bearer {token}"} if token else {}

    def file(self, url: str, name: str, *, hf: bool = False) -> Path:
        dest = self.dir / name
        if not dest.exists():
            tmp = dest.with_suffix(dest.suffix + ".part")
            with self.http.stream("GET", url, headers=self._hf_auth() if hf else {}) as r:
                if r.status_code in (401, 403):
                    raise SourceError(f"refused ({r.status_code}): {url.split('?')[0]} needs access (see the source's notes)")
                r.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
            tmp.rename(dest)
        self.revisions.append(f"{name}:{hashlib.sha256(dest.read_bytes()).hexdigest()[:12]}")
        return dest

    def hf_file(self, dataset: str, revision: str, path: str) -> Path:
        return self.file(f"{HF}/datasets/{dataset}/resolve/{revision}/{path}", f"{revision[:12]}-{Path(path).name}", hf=True)


def capped(samples: list[Sample], cap: int | None, by=lambda s: (s.label, s.category)) -> list[Sample]:
    """At most cap samples, the same share from every stratum, chosen by hash (the same ones every time)."""
    if not cap or len(samples) <= cap:
        return samples
    groups: dict = defaultdict(list)
    for s in sorted(samples, key=lambda s: hashlib.sha256(s.ext_id.encode()).hexdigest()):
        groups[by(s)].append(s)
    out, share = [], cap / len(samples)
    for g in groups.values():
        out += g[:max(1, round(len(g) * share))]
    return out[:cap]


# ---------------------------------------------------------------- the modules

def _deepset(f: Fetcher) -> Iterator[Sample]:
    import duckdb
    ds = "deepset/prompt-injections"
    rev = f.hf_revision(ds)
    f.revisions.append(rev[:12])
    for split, path in (("train", "data/train-00000-of-00001-9564e8b05b4757ab.parquet"),
                        ("test", "data/test-00000-of-00001-701d16158af87368.parquet")):
        p = f.hf_file(ds, rev, path)
        for i, (text, label) in enumerate(duckdb.sql(f"select text, label from read_parquet('{p}')").fetchall()):
            yield Sample(f"{split}-{i}", "attack" if label == 1 else "benign", text or "", category=split, kind="prompt")


def _llmail(f: Fetcher) -> Iterator[Sample]:
    ds = "microsoft/llmail-inject-challenge"
    rev = f.hf_revision(ds)
    f.revisions.append(rev[:12])
    labelled = json.loads(f.hf_file(ds, rev, "data/labelled_unique_submissions_phase2.json").read_text(encoding="utf-8"))
    for text, lab in labelled.items():
        # Only submissions judged attacks: those judged "not an attack" were still written for an injection
        # challenge (many carry chat markup or hidden text), so they are no trustworthy ordinary text; "Unclear" too.
        if str(lab.get("attack_attempt")) == "True":
            yield Sample("p2-" + hashlib.sha256(text.encode()).hexdigest()[:16], "attack", text,
                         category=f"phase2:{lab.get('reason')}", kind="email", path_hint="message.eml")
    fp = json.loads(f.hf_file(ds, rev, "data/emails_for_fp_tests.json").read_text(encoding="utf-8"))
    for i, text in enumerate(fp):
        yield Sample(f"fp-{i}", "benign", text if isinstance(text, str) else json.dumps(text), category="fp-tests",
                     kind="email", path_hint="message.eml")


def _repo_dataset(f: Fetcher) -> Iterator[Sample]:
    ds = "prodnull/prompt-injection-repo-dataset"
    rev = f.hf_revision(ds)
    f.revisions.append(rev[:12])
    for i, line in enumerate(f.hf_file(ds, rev, "train.jsonl").read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        r = json.loads(line)
        text = r.get("text") or r.get("content") or ""
        label = r.get("label")
        yield Sample(str(r.get("id", i)), "attack" if label in (1, "1", "malicious", True) else "benign", text,
                     category=r.get("category") or r.get("attack_category"), kind="repo-file",
                     path_hint=r.get("file_path") or r.get("filename") or r.get("file_type"))


def _aib(f: Fetcher) -> Iterator[Sample]:
    url = f"{GH_RAW}/sentinelden/agent-injection-bench/HEAD/aib/data/attacks.jsonl"
    for line in f.file(url, "attacks.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            yield Sample(r["id"], "benign" if r.get("family") == "control" else "attack", r.get("injected") or "",
                         category=r.get("family"), kind=r.get("channel") or "document")


def _ajb(f: Fetcher) -> Iterator[Sample]:
    url = f"{GH_RAW}/ppradyoth/AgentInjectionBench/HEAD/data/agent_injection_bench.jsonl"
    for line in f.file(url, "agent_injection_bench.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        # what the agent reads from outside: the tool, MCP, API and document turns of the conversation
        parts = [str(m.get("content") or "") for m in r.get("conversation") or []
                 if isinstance(m, dict) and m.get("role") not in ("user", "system", "assistant")]
        text = "\n\n".join(p for p in parts if p) or "\n\n".join(str(m.get("content") or "") for m in r.get("conversation") or []
                                                              if isinstance(m, dict))
        yield Sample(r["id"], "attack" if r.get("ground_truth") == "unsafe" else "benign", text,
                     category=r.get("attack_category"), kind=r.get("injection_surface") or "tool-output")


TEXT_SUFFIXES = {".py", ".js", ".md", ".sql", ".json", ".toml", ".css", ".html", ".sh", ".txt", ".yml", ".yaml"}


def _talos_code(f: Fetcher) -> Iterator[Sample]:
    """Talos's own files as ordinary text, in chunks of 60 lines: real code, docs and data, none of it an attack."""
    repo = personal.REPO
    files = sorted(p for p in repo.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES
                   and not {".git", ".venv", "node_modules", "__pycache__"} & set(p.parts))
    f.revisions.append("files:" + hashlib.sha256("".join(str(p) for p in files).encode()).hexdigest()[:12])
    for p in files:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        rel = str(p.relative_to(repo))
        for start in range(0, len(lines), 60):
            chunk = "\n".join(lines[start:start + 60])
            if chunk.strip():
                yield Sample(f"{rel}:{start + 1}", "benign", chunk, category=p.suffix.lstrip("."), kind="code", path_hint=rel)


SOURCES = {s.id: s for s in [
    Source("repo-files", "Prompt injection in repository files (prodnull)", "Apache-2.0",
           f"{HF}/datasets/prodnull/prompt-injection-repo-dataset", None, _repo_dataset,
           "Gated: accept its terms on Hugging Face, and store a token as the Keychain item huggingface-token."),
    Source("deepset", "deepset prompt-injections", "Apache-2.0", f"{HF}/datasets/deepset/prompt-injections", None, _deepset),
    Source("llmail-inject", "LLMail-Inject (Microsoft), phase 2, judged", "MIT",
           f"{HF}/datasets/microsoft/llmail-inject-challenge", 20000, _llmail,
           "Attempts judged as attacks (those judged not, or unclear, are left out: no trustworthy ordinary text), and the"
           " challenge's false-positive e-mails as ordinary e-mail."),
    Source("agent-injection-bench", "agent-injection-bench (sentinelden)", "MIT",
           "https://github.com/sentinelden/agent-injection-bench", None, _aib),
    Source("agentinjectionbench", "AgentInjectionBench (ppradyoth)", "Apache-2.0",
           "https://github.com/ppradyoth/AgentInjectionBench", None, _ajb),
    Source("talos-code", "Talos's own code and docs (ordinary text)", "MIT", "this repository", 5000, _talos_code,
           "Benign by construction: measures false alarms on real code."),
]}
