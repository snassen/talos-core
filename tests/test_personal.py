"""The owner's own configuration (talos.personal): TALOS_HOME/config wins over the repository's examples,
the tests never see theirs (conftest points TALOS_HOME at a temporary folder), and the repository
holds none of it."""

import hashlib
import json
import re
import subprocess

from talos import accounts, jev, personal


def test_his_file_wins_when_it_exists_and_the_example_otherwise(tmp_path, monkeypatch):
    monkeypatch.setenv("TALOS_HOME", str(tmp_path))
    example = tmp_path / "example.json"
    assert personal.find("structure.json", example) == example
    assert personal.text("recipient.txt", "generic") == "generic"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "structure.json").write_text("{}")
    (tmp_path / "config" / "recipient.txt").write_text("Exactly this.\n")
    assert personal.find("structure.json", example) == tmp_path / "config" / "structure.json"
    assert personal.text("recipient.txt", "generic") == "Exactly this."     # one trailing newline dropped


def test_the_tests_run_on_the_examples_never_on_the_owners_configuration():
    assert "tmp" in str(personal.home()).lower() or "talos-test-home" in str(personal.home())
    assert jev.RECIPIENT == jev.GENERIC_RECIPIENT
    ex = json.loads((personal.EXAMPLES / "accounts.example.json").read_text())
    assert accounts.ACCOUNTS == ex["accounts"] and accounts.MY_ADDRESSES == ex["my_addresses"]
    assert {a["id"] for a in accounts.ACCOUNTS} >= {"gmail", "work", "teams", "local"}


# Values that must never come back into the repository, as hashes: the Entra tenant and client IDs, the
# tailnet's name, the children's school domain and three internal addresses. Stored hashed, so this
# test does not itself put them back.
FORBIDDEN = {"0a9fe71218b9b31f", "45d480911bdbc3cf", "48ee54eb6aeb45c0", "5e2e18be944f8988", "d54ca181a149ee0e", "e3271c6f2531cd20", "f0e44a3ea67a2658"}
CANDIDATES = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|tail[0-9a-f]{6,}"
                        r"|\b(?:\d{1,3}\.){3}\d{1,3}\b|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.se\b")


def test_the_repository_holds_none_of_the_owners_configuration():
    tracked = subprocess.run(["git", "-C", str(personal.REPO), "ls-files"], capture_output=True, text=True).stdout.split()
    if not tracked:  # not a git checkout (an sdist): nothing to check
        return
    for gone in ("rules/discovery/systems.json", "rules/talos-rules-2026-09-27.json", "docs/status-2026-09-24.md"):
        assert gone not in tracked
    found = set()
    for f in tracked:
        if f.endswith((".py", ".json", ".md", ".js", ".sql", ".css", ".html", ".toml")):
            text = (personal.REPO / f).read_text(encoding="utf-8", errors="ignore")
            for m in CANDIDATES.finditer(text):
                if hashlib.sha256(m.group(0).encode()).hexdigest()[:16] in FORBIDDEN:
                    found.add(f)
    assert not found, f"the owner's IDs, tailnet, school domain or internal addresses are back in {sorted(found)}"


def test_config_init_makes_the_personal_part_from_the_examples_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("TALOS_CONFIG", str(tmp_path / "mine"))
    assert personal.home() == tmp_path / "mine"                      # TALOS_CONFIG wins over TALOS_HOME/config
    made = personal.init()
    assert set(made.values()) == {"created"} and set(made) == set(personal.FILES) | {"README.md"}
    accounts_file = tmp_path / "mine" / "accounts.json"
    assert json.loads(accounts_file.read_text()) == json.loads((personal.EXAMPLES / "accounts.example.json").read_text())
    assert (tmp_path / "mine" / "recipient.txt").read_text().strip() == jev.GENERIC_RECIPIENT
    assert (tmp_path / "mine" / "discovery").is_dir()
    accounts_file.write_text('{"accounts": [], "my_addresses": {}}')
    again = personal.init()
    assert set(again.values()) == {"kept"} and accounts_file.read_text() == '{"accounts": [], "my_addresses": {}}'
    assert personal.find("accounts.json", personal.EXAMPLES / "accounts.example.json") == accounts_file


def test_where_maps_every_place_names_keychain_items_without_reading_them_and_marks_examples(
        tmp_path, monkeypatch, database):
    from talos import secrets, where
    from talos.config import Settings
    read = []
    monkeypatch.setattr(secrets, "get", lambda k: read.append(k))
    monkeypatch.setattr(secrets, "exists", lambda k: k == "typesafe-api-key")
    monkeypatch.setenv("TALOS_HOME", str(tmp_path))
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "x").write_bytes(b"12345")
    sections = {s["title"]: s for s in where.map_all(Settings(home=tmp_path, dsn=database))}
    assert set(sections) == {"The code", "Your personal part", "The data folder (TALOS_HOME)", "The database (TALOS_DSN)",
                             "The Keychain (service \"talos\")", "The services (launchd)", "Backups"}
    mine = {i["name"]: i for i in sections["Your personal part"]["items"]}
    assert mine["accounts.json"]["state"] == "example in use" and mine["recipient.txt"]["state"] == "generic in use"
    vault = next(i for i in sections["The data folder (TALOS_HOME)"]["items"] if i["name"] == "vault")
    assert vault["value"] == "5 B"
    keys = {i["name"]: i["state"] for i in sections["The Keychain (service \"talos\")"]["items"]}
    assert keys["typesafe-api-key"] == "present" and keys["web:totp"] == "absent" and read == []   # never read
    db = {i["name"]: i["value"] for i in sections["The database (TALOS_DSN)"]["items"]}
    assert "messages" in db and "your own decisions" in db
    md = where.format_markdown(list(sections.values()))
    assert md.startswith("# Where everything is") and "| accounts.json | example in use |" in md


def test_launchd_jobs_carry_the_settings_that_are_not_the_defaults(monkeypatch):
    from talos import cli
    for k in ("TALOS_HOME", "TALOS_DSN", "TALOS_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    assert cli._launchd_env() == ""
    monkeypatch.setenv("TALOS_CONFIG", "/Users/x/My Config & more")
    env = cli._launchd_env()
    assert "<key>TALOS_CONFIG</key><string>/Users/x/My Config &amp; more</string>" in env
    assert "{env}" in cli.LAUNCHD and "EnvironmentVariables" in cli.LAUNCHD.format(exe="t", logs="l", env=env, prefix="local.talos")
