"""The owner's own configuration, kept out of the repository: TALOS_HOME/config.

The repository holds the code and generic examples. What describes the owner lives beside the vault,
outside git: who they are to Talos (owner.json), their accounts and addresses (accounts.json), the
paragraph Jev is told about them (recipient.txt) and their own wording of the questions
(questions.json), their taxonomy and mailbox structure (taxonomy.json, structure.json), their rules
(rules/), the services of their own Argus watches (argus-services.json), and the discovery drafts mined
from their mail (discovery/). No personal data is in the code.

Each loader asks find() for its file and gets the owner's when it exists, else the repository's
example, so a fresh checkout, the demo and the tests run on the examples. The tests point TALOS_HOME at
a temporary folder (tests/conftest.py), so they never read or write the owner's.

The folder can live anywhere: TALOS_CONFIG names it (for example a checkout of a private git
repository of one's own), else it is TALOS_HOME/config. `talos config init` makes it from the
examples, never overwriting a file (init()); docs/install.md walks through both ways.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "rules"
# What a personal folder holds, and where each file's starting point comes from (None: written by init).
FILES = {
    "owner.json": EXAMPLES / "owner.example.json",
    "accounts.json": EXAMPLES / "accounts.example.json",
    "recipient.txt": None,
    "structure.json": EXAMPLES / "structure.json",
    "rules/my-rules.json": EXAMPLES / "example-rules.json",
}
README = """# Talos: my own configuration

This folder is the personal part of Talos (talos.personal). The code reads it from TALOS_CONFIG, or
from TALOS_HOME/config. Keep it out of the public code: as a private git repository of its own, or
only on this machine (it is in the data folder's backups either way). Never put a password here:
secrets go in the macOS Keychain.

- owner.json      who you are to Talos: the name your own decisions are stored under, your name in
                  what Jev reads, the prefix of your background services' labels
- accounts.json   the accounts Talos syncs (ids, providers, hosts, the Keychain item each needs)
                  and every address that is "me"
- recipient.txt   who you are, in a few sentences: sent to Jev with every question (every word is paid
                  for; changing it changes the question version, so earlier answer-key checks no
                  longer count for the new questions)
- structure.json  where every mail should go under Talos/ (talos structure plan)
- rules/          your rules (talos rules load rules/<file>.json)
- discovery/      drafts of your systems and setup, for Tune › Systems (talos discovery load)

Optional, each only when you want your own instead of the repository's:

- questions.json        your own wording of the questions Jev is asked (key: text; the keys are in
                        talos.personal.WORDING); part of the question version, like recipient.txt
- taxonomy.json         your own value lists (else rules/taxonomy.json); talos setup loads it
- argus-services.json   more services for Argus to watch on day one (a list, as talos.argus.DAY_ONE)
- AGENTS.md             your rules for coding agents on your instance (which accounts may be written to,
                        what needs your go); CLAUDE.md and the talos-dev skill tell agents to read it first
"""
# The owner when owner.json is absent: "you", as the generic recipient text says.
OWNER_DEFAULT = {"id": "owner", "name": "You", "service_prefix": "local.talos", "sent_by_key": "sent_by_you"}
# The question texts questions.json may replace, by key (talos.jev, talos.focus).
WORDING = ("jev.origin", "jev.ask.statement", "jev.ask.choice", "jev.ask.none", "jev.ask.deadline",
           "focus.sender_kind", "focus.kind")


def home() -> Path:
    """TALOS_CONFIG, else TALOS_HOME/config (read from the environment each time, as talos.config does)."""
    if os.environ.get("TALOS_CONFIG"):
        return Path(os.environ["TALOS_CONFIG"]).expanduser()
    return Path(os.environ.get("TALOS_HOME", Path.home() / "TalosData")).expanduser() / "config"


def init(directory: Path | None = None) -> dict[str, str]:
    """Make the personal folder from the examples: what is missing is created, nothing is overwritten.
    Returns, per file, "created" or "kept"."""
    root = Path(directory).expanduser() if directory else home()
    out = {}
    for name, example in {**FILES, "README.md": None}.items():
        dest = root / name
        if dest.exists():
            out[name] = "kept"
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if name == "README.md":
            dest.write_text(README, encoding="utf-8")
        elif example is None:
            from talos.jev import GENERIC_RECIPIENT
            dest.write_text(GENERIC_RECIPIENT + "\n", encoding="utf-8")
        else:
            shutil.copyfile(example, dest)
        out[name] = "created"
    (root / "discovery").mkdir(parents=True, exist_ok=True)
    return out


def find(name: str, example: Path | None = None) -> Path | None:
    """The owner's file (TALOS_HOME/config/<name>) when it exists, else the example (which may be None)."""
    mine = home() / name
    return mine if mine.exists() else example


def data(name: str, default):
    """The owner's JSON file, parsed, else default."""
    p = home() / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def owner() -> dict:
    """Who the owner is to Talos (owner.json over OWNER_DEFAULT): id, the name stored with their own
    decisions (decided_by, labeller, by); name, how a record names them to Jev; service_prefix, the
    launchd labels (<prefix>.sync, .web, .teams); sent_by_key, a record's flag for mail they sent."""
    mine = data("owner.json", None) or data_example("owner.json")
    return {**OWNER_DEFAULT, **{k: v for k, v in mine.items() if k in OWNER_DEFAULT}}


def data_example(name: str) -> dict:
    ex = FILES.get(name)
    return json.loads(ex.read_text(encoding="utf-8")) if ex and ex.exists() else {}


def wording(key: str, default: str) -> str:
    """The owner's wording of one question text (questions.json), else the repository's."""
    if key not in WORDING:
        raise KeyError(f"no question text {key!r}")
    return data("questions.json", {}).get(key, default)


def text(name: str, default: str) -> str:
    """The owner's text file's contents, exactly as written (a trailing newline dropped), else default."""
    p = home() / name
    if not p.exists():
        return default
    return p.read_text(encoding="utf-8").removesuffix("\n")


# The name the owner's own decisions are stored under (decided_by, labeller, by, made_by).
OWNER_ID = owner()["id"]
