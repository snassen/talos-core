"""A message's HTML for the reading pane: cleaned here, shown in a sandboxed frame.

Mail is untrusted input (CLAUDE.md, promise 6), so the HTML of a message never enters the
app's own page. The page shows it in an <iframe sandbox="allow-popups
allow-popups-to-escape-sandbox">: no script runs in it and it has an opaque origin, so it can
read nothing of Talos. The frame loads GET /api/messages/{id}/html, which is served under a
Content-Security-Policy that allows no script, no fetch, no font or frame from anywhere, no form
and no <base>; images only as data: URLs, from this app's /api/attachments/ (the message's own
inline pictures), and from https: only when the owner asks for the remote images.

This module is the second wall, in case a browser ever lets something through the first: an
allow-list cleaner over a real HTML5 parser (selectolax's Lexbor, already a dependency, so the
tree is the one a browser builds). It keeps known formatting tags and attributes and nothing
else: script, iframe, object, embed, form controls, meta (a refresh), link, base, svg and math
go with their content; unknown tags (Outlook's o:p, a form) are unwrapped; every on* handler,
srcset and ping goes; a link keeps only an http, https, mailto or tel address and opens in a new
tab without an opener or a referrer; comments go (Outlook's conditional ones hold markup).

**Remote images are blocked by default**, because they are how a sender learns that and when a
message was read (a tracking pixel), and where from. Blocked, an <img> loses its src and CSS
url()s become none; they are counted, and those of at most 2×2 pixels are counted as trackers.
With images=True remote http: images are asked for over https: (the only remote scheme the CSP
allows). **Inline pictures** (cid:) point at the message's own attachments, served by
/api/attachments/{id}.

CSS is kept (mail is laid out with it): <style> and style="" are scrubbed of @import,
expression(), behavior, -moz-binding and url()s that are not allowed. CSS can hide a URL in an
escape the regular expressions do not read (u\\72l(…), image-set("…")); such a fetch is still
stopped by the CSP, which is the guarantee; the scrub is defence in depth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

import psycopg
from selectolax.lexbor import LexborHTMLParser

from talos import mime
from talos.vault import Vault

SANDBOX = "allow-popups allow-popups-to-escape-sandbox"

# Removed together with everything inside them.
DROP = frozenset("""
    script noscript template iframe frame frameset noframes object embed applet param portal fencedframe
    input button select textarea option optgroup datalist keygen output
    meta link base title svg math audio video source track canvas map area dialog slot
    xmp plaintext noembed xml
""".split())
# Kept, with the attributes below. Anything not here and not in DROP is unwrapped: the tag goes,
# its content stays. A <form> is unwrapped rather than dropped (some mail wraps its whole body in
# one); its controls go.
KEEP = frozenset("""
    html head body style div span p br hr a img table thead tbody tfoot tr td th caption colgroup col
    b i u s strong em small big sub sup font center blockquote pre code tt kbd samp var ul ol li dl dt dd
    h1 h2 h3 h4 h5 h6 abbr acronym address cite q del ins mark strike section article header footer nav
    main aside figure figcaption wbr details summary time bdi bdo ruby rt rp nobr label
""".split())
ATTRS = frozenset("""
    style class dir lang title align valign width height bgcolor color border cellpadding cellspacing
    colspan rowspan face size alt nowrap role summary scope headers abbr span start type reversed value
    clear hspace vspace frame rules datetime open
""".split())
URL_ATTRS = frozenset({"href", "src", "background"})
LINK_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
DATA_IMAGE = re.compile(r"data:image/(png|gif|jpe?g|webp|bmp);", re.I)
TRACKER_MAX = 2  # pixels: an image this small is there to be fetched, not seen

_SCHEME = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*):")
_TAB_NL = re.compile(r"[\t\n\r]")
_CSS_IMPORT = re.compile(r"@import\b[^;]*;?", re.I)
_CSS_URL = re.compile(r"url\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^)\"'\s]*))\s*\)", re.I)
_CSS_BAD = re.compile(r"expression\s*\(|behaviou?r\s*:|-moz-binding|javascript\s*:|vbscript\s*:", re.I)
_NUMBER = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(px)?\s*$", re.I)

# The frame's own defaults, before the message's styles so those win. Mail HTML is written for
# a white page, so the frame is light whatever the app's theme.
BASE_CSS = (":root{color-scheme:light}html{background:#fff;color:#1a1a1a}"
            "body{margin:0;padding:14px 16px;font:14px/1.5 -apple-system,system-ui,'Segoe UI',Roboto,sans-serif;"
            "overflow-wrap:break-word}img{max-width:100%;height:auto}pre{white-space:pre-wrap}")


@dataclass
class Clean:
    html: str
    remote_images: int = 0   # remote images (img, background, CSS url()) found: blocked unless allowed
    trackers: int = 0        # of those, images of at most TRACKER_MAX × TRACKER_MAX pixels
    inline_images: int = 0   # cid: pictures pointed at the message's own attachments


def _browser_url(value: str) -> str:
    """The URL as a browser reads the attribute: tabs and newlines removed, and leading and
    trailing spaces and control characters stripped (java&#9;script: is javascript:)."""
    return _TAB_NL.sub("", value).strip("\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f"
                                        "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f ")


def _scheme(url: str) -> str | None:
    m = _SCHEME.match(url)
    return m.group(1).lower() if m else None


def _cid_key(url: str) -> str:
    return unquote(url[4:]).strip().strip("<>").lower()


class _Cleaner:
    def __init__(self, cids: dict[str, str], allow_remote: bool):
        self.cids = cids
        self.allow_remote = allow_remote
        self.remote = 0
        self.trackers = 0
        self.inline = 0

    # ---- URLs
    def image_url(self, raw: str, *, tiny: bool = False) -> str | None:
        """What an image address becomes: an attachment URL for cid:, itself for a data: image,
        https for a remote one when allowed, else None (removed)."""
        url = _browser_url(raw)
        scheme = _scheme(url)
        if scheme == "cid":
            target = self.cids.get(_cid_key(url))
            if target:
                self.inline += 1
            return target
        if scheme == "data":
            return url if DATA_IMAGE.match(url) else None
        if scheme in ("http", "https") or url.startswith("//"):
            self.remote += 1
            if tiny:
                self.trackers += 1
            if not self.allow_remote:
                return None
            if url.startswith("//"):
                return "https:" + url
            return "https:" + url[len(scheme) + 1:] if scheme == "http" else url
        return None

    @staticmethod
    def link_url(raw: str) -> str | None:
        url = _browser_url(raw)
        return url if _scheme(url) in LINK_SCHEMES else None

    # ---- CSS
    def css(self, text: str) -> str:
        text = _CSS_IMPORT.sub("", text)

        def url(m: re.Match) -> str:
            got = self.image_url(next(g for g in m.groups() if g is not None))
            return 'url("' + got.replace('"', "%22").replace("\\", "%5C") + '")' if got else "none"

        text = _CSS_URL.sub(url, text)
        return _CSS_BAD.sub("", text)

    # ---- elements
    def attrs(self, node) -> None:
        tag = node.tag.lower()
        attrs = node.attrs
        if tag in ("html", "head"):
            for k in list(attrs.keys()):
                del attrs[k]
            return
        tiny = tag == "img" and all(_tiny(attrs.get(k)) for k in ("width", "height"))
        for k in list(attrs.keys()):
            name = k.lower()
            v = attrs[k] or ""
            if name in URL_ATTRS:
                if name == "href" and tag == "a":
                    new = self.link_url(v)
                elif (name == "src" and tag == "img") or name == "background":
                    new = self.image_url(v, tiny=tiny)
                else:
                    new = None
                if new is None:
                    del attrs[k]
                else:
                    attrs[k] = new
            elif name == "style":
                attrs[k] = self.css(v)
            elif name not in ATTRS and not name.startswith("aria-"):
                del attrs[k]
        if tag == "a":
            attrs["target"] = "_blank"
            attrs["rel"] = "noopener noreferrer"

    def tree(self, root) -> None:
        """Walk the document without recursion (mail can nest thousands deep): drop, unwrap or
        keep each node, then do the same inside what was kept."""
        stack = [root]
        self.attrs(root)
        while stack:
            child = stack.pop().child
            while child is not None:
                nxt = child.next
                if child.is_element_node:
                    tag = (child.tag or "").lower()
                    if tag in DROP:
                        child.decompose()
                    elif tag not in KEEP:
                        first = child.child
                        child.unwrap()
                        if first is not None:
                            child = first
                            continue
                    elif tag == "style":
                        css = self.css(child.text(deep=True)).replace("<", "")
                        for part in list(child.iter(include_text=True)):
                            part.decompose()
                        if css.strip():
                            child.insert_child(css)
                        self.attrs(child)
                    else:
                        self.attrs(child)
                        stack.append(child)
                elif not child.is_text_node:
                    child.decompose()   # comments (Outlook's conditional ones hold markup), doctypes
                child = nxt


def _tiny(v: str | None) -> bool:
    m = _NUMBER.match(v or "")
    return bool(m) and float(m.group(1)) <= TRACKER_MAX


def sanitize(html: str, *, cids: dict[str, str] | None = None, allow_remote: bool = False) -> Clean:
    """The HTML cleaned to the allow-list, as a complete document for the frame.

    cids maps a Content-ID (lower case, without <>) to the URL of the message's own attachment;
    an unknown cid: is removed. allow_remote lets remote images load (over https)."""
    c = _Cleaner(cids or {}, allow_remote)
    tree = LexborHTMLParser(html or "")
    root = tree.root
    if root is None:
        return Clean(_document(""))
    c.tree(root)
    return Clean(_document(root.html or ""), c.remote, c.trackers, c.inline)


def _document(cleaned: str) -> str:
    head = ('<meta charset="utf-8"><meta name="referrer" content="no-referrer">'
            f"<style>{BASE_CSS}</style>")
    if cleaned.startswith("<html><head>"):
        return "<!doctype html><html><head>" + head + cleaned[len("<html><head>"):]
    return f"<!doctype html><html><head>{head}</head><body>{cleaned}</body></html>"


def csp(image_base: str, *, allow_remote: bool) -> str:
    """The frame's Content-Security-Policy. image_base is this app's attachment URL prefix
    (https://host/api/attachments/), so an inline picture can load and nothing else of the app."""
    img = f"data: {image_base}" + (" https:" if allow_remote else "")
    return (f"default-src 'none'; img-src {img}; style-src 'unsafe-inline'; font-src data:;"
            f" base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox {SANDBOX}")


# ---------------------------------------------------------------- a message's HTML

def _source(conn: psycopg.Connection, vault: Vault, message_id: int) -> str | None:
    """The message's own HTML from its original in the vault; None for a Teams message (its
    original is JSON: it has a chat view of its own) or a message without HTML. Raises
    BlobUnavailable when the original was removed."""
    row = conn.execute("select m.raw_sha256, b.kind from message m join blob b on b.sha256 = m.raw_sha256"
                       " where m.id = %s", (message_id,)).fetchone()
    if not row or row["kind"] != "raw":
        return None
    return mime.html_body(vault.get(row["raw_sha256"], "raw"))


def inline_images(conn: psycopg.Connection, message_id: int, url: str = "/api/attachments/{}") -> dict[str, str]:
    """The message's pictures by Content-ID (lower case, without <>): the URL each is served at."""
    rows = conn.execute("select id, content_id from attachment where message_id = %s and content_id is not null"
                        " and blob_sha256 is not null and content_type like 'image/%%'", (message_id,)).fetchall()
    return {r["content_id"].strip().strip("<>").lower(): url.format(r["id"]) for r in rows}


def render(conn: psycopg.Connection, vault: Vault, message_id: int, *, allow_remote: bool = False) -> Clean | None:
    """The message's HTML, cleaned, for its frame; None when it has none."""
    html = _source(conn, vault, message_id)
    if not html:
        return None
    return sanitize(html, cids=inline_images(conn, message_id), allow_remote=allow_remote)


def summary(conn: psycopg.Connection, vault: Vault, message_id: int) -> dict | None:
    """For the message pane: whether there is HTML to show, and what its frame would block."""
    clean = render(conn, vault, message_id)
    if clean is None:
        return None
    return {"remote_images": clean.remote_images, "trackers": clean.trackers, "inline_images": clean.inline_images}
