"""A message's HTML in the reading pane (talos.mailhtml, GET /api/messages/{id}/html), and the icons.

The HTML is shown in a sandboxed frame under a CSP without script; the cleaner here is the second
wall. Everything is invented mail; the hostile HTML is the kind real phishing and newsletters send.
"""

import json
import re
import shutil
import subprocess
from email.message import EmailMessage
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import mailfactory as mf
import pytest
from test_web import client

from talos import mailhtml, mime
from talos.ingest import Location

STATIC = Path(__file__).parent.parent / "src" / "talos" / "web" / "static"
APP_JS = STATIC / "app.js"

HOSTILE = """<!doctype html><html lang="sv" xmlns:o="urn:x"><head>
<meta http-equiv="refresh" content="0;url=https://evil.example/">
<base href="https://evil.example/"><link rel="stylesheet" href="https://evil.example/x.css">
<style>@import url(https://evil.example/i.css); body{background:url('https://track.example/bg.png')}
.logo{background:url(cid:logo@talos.test)} p{width:expression(alert(1))}</style>
<script>alert('head')</script></head>
<body onload="alert(1)" background="http://track.example/b.jpg">
<!--[if mso]><table><tr><td><![endif]-->
<p onclick="alert(1)" onmouseover="alert(2)" style="color:#333;background:url(http://track.example/p.png)">Hej <o:p>Alex</o:p></p>
<a href="javascript:alert(1)">a</a> <a href="JaVa&#x09;ScRiPt:alert(1)">b</a> <a href=" &#x0A;javascript:alert(1)">c</a>
<a href="vbscript:msgbox(1)">d</a> <a href="data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;">e</a>
<a href="https://nordvik.example/offert?id=1&amp;b=2">Offerten</a> <a href="mailto:oskar@nordvik.se">Mejla</a>
<img src="https://track.example/open.gif" width="1" height="1" alt="">
<img src="http://cdn.nordvik.example/logo.png" alt="Nordvik" srcset="https://cdn.nordvik.example/logo2.png 2x" onerror="alert(3)">
<img src="cid:logo@talos.test" alt="inline"><img src="cid:nobody@talos.test"><img src="data:image/png;base64,iVBORw0KGgo=">
<img src="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=">
<iframe src="https://evil.example/frame"></iframe><object data="x.swf"></object><embed src="x.swf">
<svg><script>alert(4)</script></svg><math><mi>x</mi></math>
<form action="https://evil.example/login" method="post"><label>Lösenord</label><input type="password" name="p"><button>Logga in</button></form>
<table background="cid:logo@talos.test"><tr><td bgcolor="#eeeeee" onmouseover="x()">Cell</td></tr></table>
<noscript><img src="https://track.example/ns.gif"></noscript><template><img src="x" onerror="alert(5)"></template>
<script>document.write('<b>written</b>')</script>
</body></html>"""
CIDS = {"logo@talos.test": "/api/attachments/7"}


def _tags(html: str) -> set[str]:
    return {t.lower() for t in re.findall(r"<([a-zA-Z][\w:-]*)", html)}


def _attr_names(html: str) -> set[str]:
    return {a.lower() for tag in re.findall(r"<[a-zA-Z][^>]*>", html) for a in re.findall(r"\s([\w:-]+)=", tag)}


# ---------------------------------------------------------------- the cleaner

def test_script_frames_objects_forms_and_every_event_handler_are_removed():
    out = mailhtml.sanitize(HOSTILE, cids=CIDS).html
    assert not _tags(out) & {"script", "iframe", "object", "embed", "form", "input", "button", "svg", "math",
                              "noscript", "template", "base", "link", "o:p"}
    assert not [a for a in _attr_names(out) if a.startswith("on")]
    assert "alert" not in out.split("</head>")[1]  # in the head only as dead CSS text, whose expression( is gone
    assert "srcset" not in out and "document.write" not in out
    assert "Hej Alex" in out and "Lösenord" in out and ">Cell<" in out  # the content stays, unwrapped
    assert "expression(" not in out and "@import" not in out


def test_a_meta_refresh_and_a_base_are_removed_and_the_frame_gets_its_own_head():
    out = mailhtml.sanitize(HOSTILE, cids=CIDS).html
    assert "http-equiv" not in out and "evil.example" not in out
    assert out.startswith('<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">')
    assert out.index(mailhtml.BASE_CSS) < out.index(".logo{")  # the message's own styles come after, so they win


def test_javascript_links_are_removed_however_they_are_spelled_and_the_rest_open_in_a_new_tab():
    out = mailhtml.sanitize(HOSTILE, cids=CIDS).html
    hrefs = re.findall(r'href="([^"]*)"', out)
    assert hrefs == ["https://nordvik.example/offert?id=1&amp;b=2", "mailto:oskar@nordvik.se"]
    assert not re.search(r"(?i)java\s*script|vbscript|data:text", out)
    links = re.findall(r"<a\b[^>]*>", out)
    assert len(links) == 7 and all('target="_blank"' in a and 'rel="noopener noreferrer"' in a for a in links)
    for bad in ("javascript:x", " JAVASCRIPT:x", "java\nscript:x", "\x01javascript:x", "jav&#x61;script:x", "/api/objects"):
        assert "href" not in mailhtml.sanitize(f'<a href="{bad}">x</a>').html.split("<body>")[1]


def test_remote_images_are_blocked_by_default_and_counted_with_their_tracking_pixels():
    c = mailhtml.sanitize(HOSTILE, cids=CIDS)
    assert "track.example" not in c.html and "cdn.nordvik.example" not in c.html
    assert "background:none" in c.html
    # the background image in <style>, the body background, the <p> style, the pixel, the logo
    assert (c.remote_images, c.trackers) == (5, 1)
    assert '<img width="1" height="1" alt="">' in c.html and '<img alt="Nordvik">' in c.html


def test_remote_images_load_only_with_the_flag_and_then_over_https():
    c = mailhtml.sanitize(HOSTILE, cids=CIDS, allow_remote=True)
    assert 'src="https://track.example/open.gif"' in c.html
    assert 'src="https://cdn.nordvik.example/logo.png"' in c.html  # http asked for over https
    assert 'background="https://track.example/b.jpg"' in c.html
    assert "url(&quot;https://track.example/p.png&quot;)" in c.html and "http://" not in c.html
    assert "evil.example" not in c.html  # @import, <link> and <base> stay out whatever the flag
    assert mailhtml.sanitize('<img src="//cdn.example/x.png">', allow_remote=True).html.count('src="https://cdn.example/x.png"') == 1


def test_inline_cid_images_point_at_the_messages_own_attachments():
    c = mailhtml.sanitize(HOSTILE, cids=CIDS)
    assert '<img src="/api/attachments/7" alt="inline">' in c.html
    assert '<table background="/api/attachments/7">' in c.html
    assert '.logo{background:url("/api/attachments/7")}' in c.html
    assert "cid:" not in c.html and c.inline_images == 3
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in c.html and "image/svg" not in c.html
    assert mailhtml.sanitize('<img src="CID:%3CLogo@Talos.test%3E">', cids=CIDS).html.count("/api/attachments/7") == 1


def test_a_style_element_cannot_be_closed_early_by_what_the_cleaner_removes():
    out = mailhtml.sanitize("<style>p{color:red}<@import x;/style><script>alert(1)</script></style><p>x</p>").html
    assert "<script" not in out and out.count("</style>") == 2  # the frame's own and this one


def test_deep_nesting_and_empty_input_do_not_break_the_cleaner():
    assert "<body></body>" in mailhtml.sanitize("").html
    deep = mailhtml.sanitize("<div>" * 5000 + "botten" + "</div>" * 5000).html
    assert "botten" in deep


def test_the_csp_allows_no_script_and_remote_images_only_when_asked():
    base = "http://127.0.0.1:7420/api/attachments/"
    off = mailhtml.csp(base, allow_remote=False)
    assert off.startswith("default-src 'none';") and "script-src" not in off
    assert f"img-src data: {base};" in off and "https:" not in off
    for part in ("style-src 'unsafe-inline'", "font-src data:", "base-uri 'none'", "form-action 'none'",
                 "frame-ancestors 'self'", "sandbox allow-popups allow-popups-to-escape-sandbox"):
        assert part in off
    assert "allow-scripts" not in off and "allow-same-origin" not in off
    assert f"img-src data: {base} https:;" in mailhtml.csp(base, allow_remote=True)


# ---------------------------------------------------------------- the message's HTML part

def _html_mail(html: str, *, plain: str | None = "Hej Alex, offerten finns i HTML-versionen.",
               image: bytes | None = None, attach_html: bytes | None = None, forward: bytes | None = None) -> bytes:
    m = EmailMessage()
    m["From"] = "Oskar Nyström <oskar@nordvik.se>"
    m["To"] = mf.ME
    m["Subject"] = "Offert i HTML"
    m["Date"] = format_datetime(datetime(2026, 9, 22, 8, 14, tzinfo=timezone.utc))
    m["Message-ID"] = f"<{abs(hash(html))}@test.invalid>"
    if plain is None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(plain)
        m.add_alternative(html, subtype="html")
    if image is not None:
        m.get_payload()[1].add_related(image, maintype="image", subtype="jpeg", cid="<logo@talos.test>")
    if attach_html is not None:
        m.add_attachment(attach_html, maintype="text", subtype="html", filename="kvitto.html")
    if forward is not None:
        m.add_attachment(forward, maintype="message", subtype="rfc822")  # type: ignore[arg-type]
    return m.as_bytes()


def test_the_html_body_is_the_messages_own_html_not_an_attached_or_forwarded_one():
    inner = _html_mail("<p>Det vidarebefordrade</p>")
    raw = _html_mail("<p>Den egna</p>", attach_html=b"<p>Bilagan</p>")
    assert mime.html_body(raw).strip() == "<p>Den egna</p>"
    fwd = mf.forwarding(inner)
    assert mime.html_body(fwd) is None
    assert mime.html_body(mf.make(body="bara text")) is None
    assert mime.html_body(_html_mail("<p>Bara HTML</p>", plain=None)).strip() == "<p>Bara HTML</p>"
    assert mime.html_body(b"\x00\xff not a message") is None


# ---------------------------------------------------------------- the endpoint

def _ingest(conn, ingestor, raw, uid="h1"):
    mid = ingestor.ingest("gmail", raw, Location("[all]", uid)).message_id
    conn.commit()
    return mid


def test_the_html_endpoint_sends_the_cleaned_html_under_a_csp_without_script(conn, ingestor, vault, database):
    mid = _ingest(conn, ingestor, _html_mail(HOSTILE, image=mf.jpeg()))
    c = client(database, vault)
    r = c.get(f"/api/messages/{mid}/html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert csp.startswith("default-src 'none';") and "img-src data: http://testserver/api/attachments/;" in csp
    assert "https:" not in csp and "sandbox allow-popups allow-popups-to-escape-sandbox" in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    assert "<script" not in r.text and "track.example" not in r.text and "onload" not in r.text


def test_remote_images_are_blocked_by_default_and_allowed_only_with_images_1(conn, ingestor, vault, database):
    mid = _ingest(conn, ingestor, _html_mail(HOSTILE))
    c = client(database, vault)
    off = c.get(f"/api/messages/{mid}/html")
    on = c.get(f"/api/messages/{mid}/html", params={"images": "1"})
    assert "https:" not in off.headers["content-security-policy"] and "track.example" not in off.text
    assert "img-src data: http://testserver/api/attachments/ https:;" in on.headers["content-security-policy"]
    assert 'src="https://track.example/open.gif"' in on.text
    assert c.get(f"/api/messages/{mid}/html", params={"images": "0"}).text == off.text


def test_inline_images_are_served_from_the_attachment_endpoint(conn, ingestor, vault, database):
    mid = _ingest(conn, ingestor, _html_mail('<p>Logga:</p><img src="cid:logo@talos.test">', image=mf.jpeg()))
    aid = conn.execute("select id from attachment where message_id = %s and content_id = 'logo@talos.test'",
                       (mid,)).fetchone()["id"]
    c = client(database, vault)
    r = c.get(f"/api/messages/{mid}/html")
    assert f'<img src="/api/attachments/{aid}">' in r.text
    img = c.get(f"/api/attachments/{aid}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    detail = c.get(f"/api/messages/{mid}").json()
    assert detail["html"] == {"remote_images": 0, "trackers": 0, "inline_images": 1}


def test_the_message_pane_learns_whether_there_is_html_and_what_it_blocks(conn, ingestor, vault, database):
    html_id = _ingest(conn, ingestor, _html_mail(HOSTILE), "h1")
    text_id = _ingest(conn, ingestor, mf.make(subject="Bara text"), "h2")
    c = client(database, vault)
    assert c.get(f"/api/messages/{html_id}").json()["html"] == {"remote_images": 5, "trackers": 1, "inline_images": 0}
    assert c.get(f"/api/messages/{text_id}").json()["html"] is None
    assert c.get(f"/api/messages/{text_id}/html").status_code == 404
    assert c.get("/api/messages/999999/html").status_code == 404


def test_only_a_host_and_a_port_from_the_host_header_can_reach_the_csp(conn, ingestor, vault, database):
    mid = _ingest(conn, ingestor, _html_mail("<p>x</p>"))
    c = client(database, vault)
    assert c.get(f"/api/messages/{mid}/html", headers={"host": "testserver:1; script-src 'unsafe-inline'"}).status_code == 400
    odd = c.get(f"/api/messages/{mid}/html", headers={"host": "testserver:99999999"})
    assert "99999999" not in odd.headers["content-security-policy"]
    from types import SimpleNamespace as NS

    from talos.web.app import _image_base
    forged = NS(url=NS(netloc="127.0.0.1:1; script-src *", scheme="http"))
    assert _image_base(forged) == "'self'"
    assert _image_base(NS(url=NS(netloc="talos.tail1234.ts.net", scheme="https"))) == "https://talos.tail1234.ts.net/api/attachments/"
    ok = c.get(f"/api/messages/{mid}/html", headers={"host": "testserver:7420"})
    assert "img-src data: http://testserver:7420/api/attachments/;" in ok.headers["content-security-policy"]


# ---------------------------------------------------------------- the page

def test_the_page_puts_mail_html_only_in_a_frame_sandboxed_without_scripts_or_same_origin():
    js = APP_JS.read_text(encoding="utf-8")
    code = "\n".join(line for line in js.splitlines() if not line.strip().startswith("//"))
    assert "MAIL_SANDBOX = 'allow-popups allow-popups-to-escape-sandbox'" in code
    assert "allow-scripts" not in code and "allow-same-origin" not in code
    assert js.count("h('iframe'") == 1 and "h('iframe', {sandbox: MAIL_SANDBOX," in js


def test_the_mail_frame_the_page_builds_carries_the_sandbox_before_its_source(tmp_path):
    """mailFrame() run against a stand-in DOM: the iframe has the sandbox, set before src (so the
    first load is already sandboxed), and asks for remote images only when told to."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = APP_JS.read_text(encoding="utf-8")
    grab = lambda pattern: re.search(pattern, js, re.S | re.M).group(0)
    code = "\n".join([grab(r"^function add\(.*?^}\n"), grab(r"^function h\(.*?^}\n"),
                      grab(r"^const MAIL_SANDBOX = .*?;\n"), grab(r"^function mailFrame\(.*?^}\n")])
    fake = """
class El { constructor(tag) { this.tag = tag; this.attrs = []; this.kids = []; this.style = {}; }
  setAttribute(k, v) { this.attrs.push([k, String(v)]); } addEventListener() {} append(k) { this.kids.push(k); } }
globalThis.Node = El;
globalThis.document = {createElement: t => new El(t), createTextNode: t => ({text: t})};
"""
    run = """
const out = [false, true].map(images => { const wrap = mailFrame({id: 42}, images, false); const f = wrap.kids[0];
  return {wrap: wrap.tag, tag: f.tag, attrs: f.attrs}; });
console.log(JSON.stringify(out));
"""
    (tmp_path / "t.js").write_text(fake + code + run, encoding="utf-8")
    got = json.loads(subprocess.run([node, str(tmp_path / "t.js")], capture_output=True, text=True, check=True).stdout)
    for frame, src in zip(got, ("/api/messages/42/html", "/api/messages/42/html?images=1")):
        names = [k for k, _ in frame["attrs"]]
        attrs = dict(frame["attrs"])
        assert frame["tag"] == "iframe" and attrs["sandbox"] == "allow-popups allow-popups-to-escape-sandbox"
        assert names.index("sandbox") < names.index("src") and attrs["src"] == src
        assert attrs["referrerpolicy"] == "no-referrer"


# ---------------------------------------------------------------- the icons

def test_the_shell_links_the_icons_and_the_manifest_with_fingerprints(database, vault):
    c = client(database, vault)
    page = c.get("/").text
    refs = dict(re.findall(r'href="/static/([^"?]+)\?v=([0-9a-f]{12})"', page))
    assert {"icons/beacon-tab.svg", "icons/beacon-tab.ico", "icons/beacon-tab-32.png",
            "icons/beacon-touch-180.png", "manifest.webmanifest", "style.css"} <= set(refs)
    assert re.search(r'src="/static/app\.js\?v=[0-9a-f]{12}"', page)
    assert re.search(r'<link rel="apple-touch-icon" href="/static/icons/beacon-touch-180\.png\?v=', page)
    assert re.search(r'<link rel="manifest" href="/static/manifest\.webmanifest\?v=', page)
    types = {"icons/beacon-tab.svg": "image/svg+xml", "icons/beacon-tab-32.png": "image/png",
             "icons/beacon-touch-180.png": "image/png", "icons/menu-home-button.svg": "image/svg+xml",
             "icons/hero-page.svg": "image/svg+xml", "manifest.webmanifest": "application/manifest+json"}
    for path, ctype in types.items():
        r = c.get(f"/static/{path}")
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype), path
    assert c.get("/static/icons/beacon-tab.ico").headers["content-type"] in ("image/x-icon", "image/vnd.microsoft.icon")


def test_the_manifest_names_talos_and_icons_that_exist():
    m = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["name"] == "Talos" and m["display"] == "standalone"
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert m["theme_color"] in css and m["background_color"] in css  # colours from the tokens
    assert {i["sizes"] for i in m["icons"]} == {"192x192", "512x512"}
    for i in m["icons"]:
        assert (STATIC / i["src"].removeprefix("/static/")).is_file()


def test_a_changed_icon_gets_a_new_url(database, vault, monkeypatch, tmp_path):
    from talos.web import app as web_app
    fake = tmp_path / "static"
    shutil.copytree(STATIC, fake)
    monkeypatch.setattr(web_app, "STATIC", fake)
    before = re.search(r"beacon-tab\.svg\?v=(\w+)", web_app._fingerprinted()).group(1)
    (fake / "icons" / "beacon-tab.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    after = re.search(r"beacon-tab\.svg\?v=(\w+)", web_app._fingerprinted()).group(1)
    assert before != after
