"""The promises Talos makes, held by the build rather than by good intentions.

1. It sends a mail only when the owner presses Send on a mail they composed, after confirming the
   sending account. The sending code lives in talos/send.py alone;
   only the web app's send route imports it, so no CLI command, sync, rule run, Argus timer or
   model step can reach it; the Mail.Send scope is named in graphauth.SEND_SCOPES and nowhere
   else; and the route sends only with a confirmation token (tests/test_compose.py).
2. It never deletes permanently.
3. Sync never writes to a server: IMAP folders are selected read-only, bodies are
   fetched without setting \\Seen, and Graph is only ever sent GET.
4. Sync asks Microsoft for read-only scopes. The write scopes (Mail.ReadWrite for write-back,
   Mail.Send for the Send button) are named only in graphauth.py and used only where they belong.
5. The UI never inserts mail content as markup.
6. Write-back (talos/writeback/), the one place that writes, still never expunges
   (not even implicitly through MOVE, CLOSE or \\Deleted), never sends, and never
   issues an HTTP DELETE.

Each rule is a pattern scan over the source. The scanner self-tests at the end prove
each rule catches what it claims to, so a broken pattern cannot pass silently.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "talos"
SEND = SRC / "send.py"
# Sending code lives in talos/send.py. Outside it, the send scope may be named only in graphauth.py
# (SEND_SCOPES, which the owner's sign-in asks for), and the only line that names the send route is the route.
SCOPE_FILES = {"graphauth.py"}
ALLOWED_SEND_LINES = {
    "app.py": ['Route("/api/drafts/{id:int}/send", draft_send, methods=["POST"]),',
               'Route("/api/teams/send", teams_post_send, methods=["POST"]),'],
}
SEND_IMPORTERS = {SRC / "web" / "app.py"}

NEVER_ANYWHERE = {
    "sends mail over SMTP": r"\bsmtplib\b|\bSMTP\(",
    "calls Graph sendMail or send": r"sendMail|/send\b|\.send_message\(",
    "expunges or permanently deletes": r"\.expunge\(|permanentDelete|delete_messages\(",
    "asks for a send scope": r"Mail\.Send|ChannelMessage\.Send|ChatMessage\.Send",
}
NEVER_IN_SYNC = {
    "writes over IMAP": r"client\.(store|move|copy|append|add_flags|remove_flags|set_flags|add_gmail_labels|"
                        r"set_gmail_labels|remove_gmail_labels|create_folder|delete_folder|rename_folder)\(",
    "fetches a body in a way that marks it read": r"['\"]RFC822['\"]|['\"]BODY\[\]['\"]\s*[\],]",
    "sends anything but GET to Graph": r"\b(httpx|http|self\.http|client|self\.client)\.(post|patch|put|delete|request|stream)\(",
    # CalDAV reads with two WebDAV methods, PROPFIND and REPORT; any other request() is refused.
    "sends a request that is not a read": r"\.request\((?!\s*\"(PROPFIND|REPORT)\")",
}
NEVER_IN_WRITEBACK = {
    "expunges": r"(?i)expunge",
    "marks a message \\Deleted, the first half of an expunge": r"\\+Deleted\b",
    "moves over IMAP (MOVE expunges the source) or closes a folder (CLOSE expunges)":
        r"\.move\(|close_folder\(",
    "deletes a message or folder over IMAP": r"delete_messages\(|delete_folder\(",
    "sends mail": r"\bsmtplib\b|\bSMTP\(|sendMail|/send\b|\.send_message\(|/reply(All)?\b|/forward\b|"
                  r"createReply|createForward",
    "deletes permanently": r"(?i)permanentDelete",
    "issues an HTTP DELETE": r"\.delete\(|['\"]DELETE['\"]",
}
NEVER_IN_UI = {"inserts markup": r"innerHTML|outerHTML|insertAdjacentHTML|document\.write"}


def scan(text: str, rules: dict[str, str]) -> list[str]:
    found = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        for what, pattern in rules.items():
            if re.search(pattern, line):
                found.append(f"line {i}: {what}: {stripped[:120]}")
    return found


def _py(folder: Path):
    return sorted(p for p in folder.rglob("*.py"))


def _code_only(path: Path) -> str:
    """The file without its docstrings, which describe the forbidden calls by name."""
    text = path.read_text(encoding="utf-8")
    return re.sub(r'"""(.|\n)*?"""', '""', text)


def _without_allowed(p: Path) -> str:
    allowed = set(ALLOWED_SEND_LINES.get(p.name, []))
    return "\n".join("" if line.strip() in allowed else line for line in _code_only(p).splitlines())


def test_nothing_but_the_send_module_can_send_mail_and_nothing_deletes_permanently():
    def rules_for(p: Path) -> dict[str, str]:
        return {k: v for k, v in NEVER_ANYWHERE.items() if not (k == "asks for a send scope" and p.name in SCOPE_FILES)}
    problems = {p.name: scan(_without_allowed(p), rules_for(p)) for p in _py(SRC) if p != SEND}
    assert {k: v for k, v in problems.items() if v} == {}
    deleting = {k: v for k, v in NEVER_ANYWHERE.items() if "delete" in k}
    assert scan(_code_only(SEND), deleting) == []


def test_each_allowed_send_line_is_there_exactly_once():
    # An allowance that no longer matches anything would quietly widen nothing, but it would hide
    # a moved line; each must be found once, where it is written down.
    for name, lines in ALLOWED_SEND_LINES.items():
        path = next(p for p in _py(SRC) if p.name == name)
        text = [line.strip() for line in _code_only(path).splitlines()]
        for line in lines:
            assert text.count(line) == 1, (name, line)


def _imports_send(path: Path) -> list[str]:
    """Every way a module names talos.send: import talos.send, from talos import send, from . import
    send (inside talos), from talos.send import …, or the dotted name as a string (importlib)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    pkg = ".".join(["talos", *path.relative_to(SRC).parent.parts]).rstrip(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name == "talos.send" or a.name.startswith("talos.send.")]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".")
                parts = parts[:len(parts) - node.level + 1]
                base = ".".join(parts + ([node.module] if node.module else []))
            if base == "talos.send" or base.startswith("talos.send."):
                found.append(base)
            elif base == "talos":
                found += ["talos." + a.name for a in node.names if a.name == "send"]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"talos\.send(\..*)?", node.value):
            found.append(node.value)
    return found


def test_only_the_web_app_imports_the_send_module():
    importers = {p for p in _py(SRC) if p != SEND and _imports_send(p)}
    assert importers == SEND_IMPORTERS


def test_no_command_job_or_other_module_can_reach_the_send_module():
    """Import every module of talos but the web app (the CLI, sync, rules, Argus, the model steps,
    write-back …) in a fresh interpreter: talos.send must not be loaded by any of them."""
    code = (
        "import importlib, pkgutil, sys, talos\n"
        "names = [m.name for m in pkgutil.walk_packages(talos.__path__, 'talos.')]\n"
        "skip = {'talos.send', 'talos.web.app'}\n"
        "for n in names:\n"
        "    if n not in skip:\n"
        "        importlib.import_module(n)\n"
        "assert 'talos.cli' in sys.modules and 'talos.argus' in sys.modules\n"
        "print('talos.send' in sys.modules)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "False"


def test_the_web_app_sends_from_one_route_only():
    """One route sends mail, one posts to Teams (decided 30 Sep 2026); each needs its one-time token first."""
    code = _code_only(SRC / "web" / "app.py")
    assert code.count("sender.send(") == 1 and code.count("sender.confirm(") == 1
    assert code.count("sender.teams_send(") == 1 and code.count("sender.teams_confirm(") == 1
    route = code[code.index("async def draft_send("):code.index("def events(")]
    assert "sender.send(" in route and "_refused_send(request)" in route
    confirm = code[code.index("async def draft_confirm("):code.index("async def draft_send(")]
    assert "_refused_send(request)" in confirm
    tsend = code[code.index("async def teams_post_send("):code.index("async def draft_confirm(")]
    assert "sender.teams_send(" in tsend and "_refused_send(request)" in tsend
    tconfirm = code[code.index("async def teams_confirm("):code.index("async def teams_post_send(")]
    assert "sender.teams_confirm(" in tconfirm and "_refused_send(request)" in tconfirm


def test_the_ui_posts_a_send_only_from_the_confirmation():
    js = (SRC / "web" / "static" / "app.js").read_text()
    sends = [m.start() for m in re.finditer(r"/send`", js)]
    assert len(sends) == 1
    body = js[js.rindex("function confirmSend(", 0, sends[0]):sends[0]]
    assert "Yes, send from" in body
    # A Teams post goes on the owner's Enter in the conversation's own box: from the composer alone, with
    # the token it was just given for exactly that target and text.
    posts = [m.start() for m in re.finditer(r"/api/teams/send'", js)]
    assert len(posts) == 1
    body = js[js.rindex("function teamsComposer(", 0, posts[0]):posts[0]]
    assert "c = await post('/api/teams/confirm', {...target, text: body}" in body
    assert "{...target, text: body, token: c.token}" in js[posts[0]:posts[0] + 60]


def test_sync_adapters_never_write_to_a_server():
    files = _py(SRC / "sources") + [SRC / "graphauth.py"]
    problems = {p.name: scan(_code_only(p), NEVER_IN_SYNC) for p in files}
    assert {k: v for k, v in problems.items() if v} == {}


def test_write_back_never_expunges_sends_or_deletes():
    files = _py(SRC / "writeback")
    assert {p.name for p in files} >= {"__init__.py", "gmail.py", "graph.py"}
    problems = {p.name: scan(_code_only(p), NEVER_IN_WRITEBACK) for p in files}
    assert {k: v for k, v in problems.items() if v} == {}


def test_only_write_back_opens_a_folder_read_write():
    for p in _py(SRC):
        if p.parent.name == "writeback":
            continue
        for line in _code_only(p).splitlines():
            if "select_folder(" in line:
                assert "readonly=True" in line, f"{p.name}: {line.strip()}"


def test_every_imap_select_is_read_only():
    for p in _py(SRC / "sources"):
        for line in _code_only(p).splitlines():
            if "select_folder(" in line:
                assert "readonly=True" in line, f"{p.name}: {line.strip()}"


def test_microsoft_scopes_are_read_only():
    text = (SRC / "graphauth.py").read_text()
    scopes = re.search(r"^SCOPES = \[(.*?)\]", text, re.M | re.S).group(1)
    assert "ReadWrite" not in scopes and "Send" not in scopes


def test_the_write_scope_is_used_only_by_the_write_back_executor():
    text = (SRC / "graphauth.py").read_text()
    write = re.search(r"^WRITE_SCOPES = \[(.*?)\]", text, re.M).group(1)
    assert "Mail.ReadWrite" in write and "Send" not in write
    uses = {p.relative_to(SRC).as_posix() for p in _py(SRC) for line in _code_only(p).splitlines()
            if "WRITE_SCOPES" in line}
    assert uses <= {"graphauth.py", "writeback/__init__.py"}, uses  # the owner consented 2026-09-26


def test_the_ui_never_inserts_markup():
    js = (SRC / "web" / "static" / "app.js").read_text()
    assert scan(js, NEVER_IN_UI) == []


def test_the_scanner_catches_what_it_claims_to():
    bad = "\n".join([
        "import smtplib",
        "client.expunge()",
        'http.post(url)',
        'client.store(uids, "+FLAGS", [b"\\\\Seen"])',
        'client.fetch(uids, ["RFC822"])',
        'client.fetch(uids, ["BODY[]", "FLAGS"])',
        'el.innerHTML = body',
    ])
    assert len(scan(bad, NEVER_ANYWHERE)) == 2
    assert len(scan(bad, NEVER_IN_SYNC)) == 4
    assert len(scan(bad, NEVER_IN_UI)) == 1
    assert scan('client.fetch(uids, ["BODY.PEEK[]"])', NEVER_IN_SYNC) == []
    # A CalDAV read may send PROPFIND or REPORT, and nothing else through request().
    assert scan('self._client().request("PROPFIND" if m else "REPORT", url)', NEVER_IN_SYNC) == []
    assert len(scan('self._client().request("PUT", url)', NEVER_IN_SYNC)) == 1
    assert len(scan('self._client().request(method, url)', NEVER_IN_SYNC)) == 1


def test_the_write_back_scanner_catches_each_thing_it_forbids():
    bad = {
        "expunges": ['client.expunge()', 'client.uid_expunge(uids)', 'client._imap.expunge'],
        "marks a message \\Deleted, the first half of an expunge": ['client.add_flags(uids, [b"\\\\Deleted"])'],
        "moves over IMAP (MOVE expunges the source) or closes a folder (CLOSE expunges)":
            ['client.move(uids, trash)', 'client.close_folder()'],
        "deletes a message or folder over IMAP": ['client.delete_messages(uids)', 'client.delete_folder(f)'],
        "sends mail": ['import smtplib', 'req("POST", "/me/sendMail", body)', 'url = f"/me/messages/{i}/send"',
                       'url = f"/me/messages/{i}/reply"', 'url = f"/me/messages/{i}/forward"'],
        "deletes permanently": ['req("POST", f"/me/messages/{i}/permanentDelete")'],
        "issues an HTTP DELETE": ['self.http.delete(url)', 'req = {"method": "DELETE", "url": url}'],
    }
    assert set(bad) == set(NEVER_IN_WRITEBACK)
    for what, lines in bad.items():
        for line in lines:
            hits = scan(line, NEVER_IN_WRITEBACK)
            assert any(f": {what}: " in h for h in hits), f"{what!r} missed {line!r}"
    allowed = [
        'client.add_flags(uids, [b"\\\\Seen"])', 'client.add_gmail_labels(uids, ["Talos/Kvitto"])',
        'client.copy(uids, trash)', 'client.select_folder(folder, readonly=False)',
        'self._req("POST", f"/me/messages/{t.provider_id}/move", body)', 'self.http.post(url, json=body)',
        'self._req("PATCH", f"/me/messages/{i}", {"isRead": True})',
    ]
    assert [h for line in allowed for h in scan(line, NEVER_IN_WRITEBACK)] == []


def test_only_the_web_app_writes_to_calendars_and_never_with_attendees():
    """talos.calwrite writes entries the owner saves in Talos Web to their real calendars (decided 30 Sep 2026).
    No sync, rule, job or model may reach it, and no write may name an attendee (that would invite
    people, which is sending mail on their behalf)."""
    imports = re.compile(r"from talos import \([^)]*\bcalwrite\b|from talos import [^\n(]*\bcalwrite\b"
                         r"|import talos\.calwrite|from talos\.calwrite")
    importers = {p.relative_to(SRC).as_posix() for p in _py(SRC) if p.name != "calwrite.py" and imports.search(_code_only(p))}
    assert importers == {"web/app.py"}, importers
    code = _code_only(SRC / "calwrite.py")
    assert not re.search(r"""["']attendees["']\s*:""", code), "a calendar write names attendees"
    assert code.count('params={"sendUpdates": "none"}') == 1 and code.count("self.http.request(") == 2
    assert "sendMail" not in code and "/cancel" not in code and "/forward" not in code
