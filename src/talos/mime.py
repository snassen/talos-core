"""Parsing one raw RFC 822 message into what Talos needs. Pure: bytes in, data out.

The parser never trusts a header to be well-formed. Every header read falls back to
a raw decode, and a message that cannot be parsed at all still yields a record with
what could be read, so ingest never loses an original over a bad header.

PARSER_VERSION is stored on every message row. Bump it when the output changes,
and re-parse from the vault (`talos reparse`); the originals make that cheap.

Version 2: text parts around inline images are joined, 8-bit headers and bodies without a
charset are read as UTF-8 or Windows-1252, forwarded messages keep their original bytes and
add their text, and each part says whether it is an attachment (`Part.is_attachment`).
"""

from __future__ import annotations

import base64
import binascii
import quopri
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from urllib.parse import unquote

from talos import textclean

PARSER_VERSION = 2

MAX_DEPTH = 40             # MIME nesting deeper than this is not walked (real mail nests < 10)
MAX_FORWARDED_DEPTH = 3    # a forwarded message inside a forwarded message inside …
MAX_FORWARDED_TEXT = 20_000  # characters of each forwarded message's text added to the body

_SURROGATES = re.compile("[\ud800-\udfff]")
_CID_REF = re.compile(r"cid:\s*([^\s\"'<>)]+)", re.I)

_NOREPLY = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notifications?|notify|alerts?|"
    r"mailer-daemon|postmaster|bounces?|automated|robot)([+-].*)?$",
    re.I,
)
_RULE_HEADERS = {
    "auto-submitted": "auto_submitted",
    "precedence": "precedence",
    "list-unsubscribe": "list_unsubscribe",
    "x-mailer": "x_mailer",
    "return-path": "return_path",
    "delivered-to": "delivered_to",
    "importance": "importance",
    "x-priority": "x_priority",
    "content-language": "content_language",
}


@dataclass(frozen=True)
class Address:
    address: str  # lower-cased
    name: str | None = None

    @property
    def domain(self) -> str:
        return self.address.rpartition("@")[2]


@dataclass
class Part:
    part_path: str
    content_type: str
    filename: str | None
    disposition: str | None
    content_id: str | None
    data: bytes
    # A file the sender attached, as opposed to a picture shown in the HTML (an image in
    # multipart/related referenced by cid:) or the calendar alternative of an invitation.
    is_attachment: bool = True
    in_related: bool = False
    in_alternative: bool = False


@dataclass
class ParsedMessage:
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    subject: str | None
    sent_at: datetime | None
    from_: Address | None
    sender: Address | None
    reply_to: list[Address]
    to: list[Address]
    cc: list[Address]
    bcc: list[Address]
    list_id: str | None
    headers: dict[str, str]
    is_automated: bool
    automated_reasons: list[str]
    body_kind: str  # plain | html | none
    body_text: str
    quote_stripped: str
    snippet: str
    size: int
    attachments: list[Part] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)


def _raw_header(msg: Message, name: str) -> str | None:
    try:
        value = msg.get(name)
    except Exception:
        value = None
        for k, v in msg.raw_items() if hasattr(msg, "raw_items") else msg.items():
            if k.lower() == name.lower():
                value = v
                break
    if value is None:
        return None
    try:
        text = str(value)
    except Exception:
        text = value if isinstance(value, str) else repr(value)
    if "=?" in text:
        try:
            text = str(make_header(decode_header(text)))
        except Exception:
            pass
    return " ".join(text.split()) or None


def _all_headers(msg: Message, name: str) -> list[str]:
    out = []
    try:
        values = msg.get_all(name) or []
    except Exception:
        values = []
    for v in values:
        try:
            out.append(str(v))
        except Exception:
            continue
    return out


def _addresses(msg: Message, name: str) -> list[Address]:
    values = _all_headers(msg, name)
    result: list[Address] = []
    seen = set()
    for display, addr in getaddresses(values):
        addr = addr.strip().strip("<>").lower()
        if "@" not in addr or addr in seen:
            continue
        seen.add(addr)
        if display and "=?" in display:
            try:
                display = str(make_header(decode_header(display)))
            except Exception:
                pass
        result.append(Address(addr, " ".join(display.split()) or None))
    return result


def _msgid(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"<([^<>\s]+)>", value)
    token = m.group(1) if m else value.strip().split()[0] if value.strip() else ""
    return token.strip("<>") or None


def _msgids(value: str | None) -> list[str]:
    if not value:
        return []
    found = re.findall(r"<([^<>\s]+)>", value)
    return found or [t.strip("<>") for t in value.split() if t.strip("<>")]


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if not (1990 <= dt.year <= 2100):
        return None
    return dt


def _decode_bytes(data: bytes, charset: str | None) -> str:
    """Text from bytes. A declared charset is trusted; without one (or with a us-ascii label
    on 8-bit bytes, or an unknown label) try UTF-8, then Windows-1252, then Latin-1, which
    is how Swedish mail from older clients is written. Never raises."""
    charset = (charset or "").strip().lower()
    if charset and charset not in ("us-ascii", "ascii"):
        try:
            return data.decode(charset)
        except LookupError:
            pass
        except UnicodeDecodeError:
            return data.decode(charset, errors="replace")
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


class _Policy(policy.EmailPolicy):
    """policy.default, except that raw 8-bit header bytes are decoded as UTF-8, then
    Windows-1252, instead of becoming U+FFFD.

    BytesParser keeps undecodable header bytes as surrogate escapes; policy.default then
    decodes them as UTF-8 with errors='replace', so "R\xe4kning" in Latin-1 became
    "R\ufffdkning". Decoding here, before the header object is built, fixes every reader:
    Subject, From, filenames in Content-Disposition and so on."""

    def header_fetch_parse(self, name, value):
        if isinstance(value, str) and _SURROGATES.search(value):
            value = _decode_bytes(value.encode("utf-8", "surrogateescape"), None)
        return super().header_fetch_parse(name, value)


_POLICY = _Policy()


def _decode_text(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True) or b""
    except Exception:
        payload = b""
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "surrogateescape")
    try:
        charset = part.get_content_charset()
    except Exception:
        charset = None
    return _decode_bytes(payload, charset)


# ---------------------------------------------------------------- raw part bytes

_HEADER_END = re.compile(rb"\r?\n\r?\n")


def _split_entity(raw: bytes) -> tuple[bytes, bytes]:
    """Headers and body of one MIME entity, as bytes."""
    if raw.startswith((b"\r\n", b"\n")):
        return b"", raw[2 if raw.startswith(b"\r\n") else 1:]
    m = _HEADER_END.search(raw)
    return (raw[:m.start()], raw[m.end():]) if m else (raw, b"")


def _split_multipart(body: bytes, boundary: str) -> list[bytes]:
    """The raw bytes of each body part, found the way the feedparser finds them: a line
    that is exactly --boundary (or --boundary--), allowing trailing blanks."""
    delim = re.escape(b"--" + boundary.encode("utf-8", "surrogateescape"))
    pattern = re.compile(rb"(?:\A|\r?\n)" + delim + rb"(--)?[ \t]*(?:\r?\n|\Z)")
    chunks, start = [], None
    for m in pattern.finditer(body):
        if start is not None:
            chunks.append(body[start:m.start()])
        if m.group(1):  # the close delimiter
            return chunks
        start = m.end()
    if start is not None:
        chunks.append(body[start:])
    return chunks


def _transfer_decode(data: bytes, cte: str) -> bytes:
    cte = (cte or "").strip().lower()
    if cte == "base64":
        try:
            # Extra padding is harmless and rescues a body cut short of its last "=".
            return base64.b64decode(re.sub(rb"[^A-Za-z0-9+/=]", b"", data) + b"==", validate=False)
        except (binascii.Error, ValueError):
            return data
    if cte == "quoted-printable":
        return quopri.decodestring(data)
    return data


# ---------------------------------------------------------------- the walk

@dataclass
class _Walk:
    depth: int  # forwarded-message depth of the message being walked
    attachments: list[Part] = field(default_factory=list)
    html: list[str] = field(default_factory=list)       # raw HTML, for cid: references
    forwarded: list[str] = field(default_factory=list)  # marked text sections
    defects: list[str] = field(default_factory=list)


def _children(part: Message) -> list[Message]:
    kids = part.iter_parts() if hasattr(part, "iter_parts") else part.get_payload()
    return list(kids or [])


def _walk(part: Message, path: str, w: _Walk, raw: bytes | None, *, level: int = 0,
          in_related: bool = False, in_alternative: bool = False) -> dict[str, str]:
    """Collect attachments into w and return this entity's text as {"plain": …, "html": …}
    (the HTML already turned into text). Text parts of a multipart/mixed or related are
    joined in order, so text after an inline photo is kept; of a multipart/alternative only
    one version of each kind is taken, so plain and HTML are never counted twice."""
    if level > MAX_DEPTH:
        w.defects.append("walk:too-deep")
        return {}
    ctype = part.get_content_type()
    if ctype == "message/rfc822":
        _forwarded(part, path or "1", w, raw)
        return {}
    if part.is_multipart():
        kids = _children(part)
        chunks: list[bytes] | None = None
        boundary = part.get_boundary()
        if raw is not None and boundary:
            found = _split_multipart(_split_entity(raw)[1], boundary)
            chunks = found if len(found) == len(kids) else None
        results = []
        for i, child in enumerate(kids, 1):
            results.append(_walk(child, f"{path}.{i}" if path else str(i), w, chunks[i - 1] if chunks else None,
                                 level=level + 1, in_related=in_related or ctype == "multipart/related",
                                 in_alternative=ctype == "multipart/alternative"))
        if ctype == "multipart/alternative":
            best: dict[str, str] = {}
            for res in results:
                for key, text in res.items():
                    if not best.get(key, "").strip():
                        best[key] = text
            return best
        joined: dict[str, list[str]] = {}
        for res in results:
            if not res:
                continue
            for key, other in (("plain", "html"), ("html", "plain")):
                piece = res.get(key) if res.get(key) is not None else res.get(other)
                joined.setdefault(key, [])
                if piece and piece.strip():
                    joined[key].append(piece.strip("\r\n"))
        kinds = {k for res in results for k in res}
        return {k: "\n\n".join(v) for k, v in joined.items() if k in kinds}
    path = path or "1"
    try:
        disposition = part.get_content_disposition()
    except Exception:
        disposition = None
    try:
        filename = part.get_filename()
    except Exception:
        filename = None
    if ctype in ("text/plain", "text/html") and disposition != "attachment" and not filename:
        text = _decode_text(part)
        if ctype == "text/plain":
            return {"plain": text}
        w.html.append(text)
        return {"html": textclean.html_to_text(text)}
    try:
        data = part.get_payload(decode=True)
    except Exception:
        data = None
    if not data:
        return {}
    cid = _raw_header(part, "Content-ID")
    w.attachments.append(Part(path, ctype, filename, disposition, cid.strip("<>") if cid else None, data,
                              in_related=in_related, in_alternative=in_alternative))
    return {}


def _forwarded(part: Message, path: str, w: _Walk, raw: bytes | None) -> None:
    """A forwarded message (message/rfc822) is stored as its original bytes, decoded from
    any transfer encoding, and its text is added to the body so a search finds it."""
    inner = part.get_payload()
    inner_msg = inner[0] if isinstance(inner, list) and inner else None
    if raw is not None:
        data = _transfer_decode(_split_entity(raw)[1], str(part.get("Content-Transfer-Encoding", "")))
    else:  # the raw bytes could not be located; fall back to re-serialising
        try:
            data = inner_msg.as_bytes() if inner_msg is not None else b""
        except Exception:
            data = b""
    sub = None
    if data and w.depth < MAX_FORWARDED_DEPTH:
        try:
            sub = _parse(data, depth=w.depth + 1)
        except Exception as exc:
            w.defects.append(f"forwarded:{type(exc).__name__}")
    try:
        filename = part.get_filename()
    except Exception:
        filename = None
    subject = sub.subject if sub else (_raw_header(inner_msg, "Subject") if inner_msg is not None else None)
    name = filename or (subject or "message") + ".eml"
    w.attachments.append(Part(path, "message/rfc822", name, "attachment", None, data))
    if sub and sub.body_text:
        who = sub.from_.name or sub.from_.address if sub.from_ else None
        head = "[Forwarded message" + (f": {subject}" if subject else "") + (f", from {who}" if who else "") + "]"
        w.forwarded.append(head + "\n" + sub.body_text[:MAX_FORWARDED_TEXT])


def _classify(attachments: list[Part], html: list[str]) -> None:
    """An attachment is a part with a filename or disposition=attachment. Not attachments:
    an image inside multipart/related that the HTML shows by cid:, and the text/calendar
    alternative of an invitation."""
    refs = {unquote(r).strip().strip("<>").lower() for h in html for r in _CID_REF.findall(h)}
    for a in attachments:
        if a.content_type == "text/calendar" and a.in_alternative and a.disposition != "attachment":
            a.is_attachment = False
        elif (a.content_type.startswith("image/") and a.in_related and a.content_id
              and a.content_id.lower() in refs):
            a.is_attachment = False
        else:
            a.is_attachment = bool(a.filename) or a.disposition == "attachment"


def _nul_free(value):
    """PostgreSQL text and jsonb cannot hold NUL or lone surrogates. Real mail sometimes has
    them, in bodies, headers and filenames. The original keeps every byte; only the derived
    text drops them."""
    if isinstance(value, str):
        # Lone surrogates (from a utf-7 "+2AA-", or an undecodable header read raw) cannot be
        # encoded as UTF-8 either; they become U+FFFD.
        return _SURROGATES.sub("\ufffd", value.replace("\x00", ""))
    if isinstance(value, list):
        return [_nul_free(v) for v in value]
    if isinstance(value, dict):
        return {k: _nul_free(v) for k, v in value.items()}
    if isinstance(value, Address):
        return Address(_nul_free(value.address), _nul_free(value.name))
    return value


def parse(raw: bytes) -> ParsedMessage:
    p = _parse(raw)
    for name in ("message_id", "in_reply_to", "references", "subject", "from_", "sender", "reply_to", "to",
                 "cc", "bcc", "list_id", "headers", "automated_reasons", "body_text", "quote_stripped",
                 "snippet", "defects"):
        setattr(p, name, _nul_free(getattr(p, name)))
    for part in p.attachments:
        part.filename = _nul_free(part.filename)
        part.content_id = _nul_free(part.content_id)
        part.content_type = _nul_free(part.content_type)
    return p


def _parse(raw: bytes, *, depth: int = 0) -> ParsedMessage:
    defects: list[str] = []
    try:
        msg = BytesParser(policy=_POLICY).parsebytes(raw)
        walk_ok = True
    except RecursionError:  # absurdly deep nesting: keep the headers, skip the body
        msg = BytesParser(policy=_POLICY).parsebytes(raw, headersonly=True)
        defects.append("parse:RecursionError")
        walk_ok = False
    defects += [type(d).__name__ for d in getattr(msg, "defects", [])]

    w = _Walk(depth=depth)
    text: dict[str, str] = {}
    if walk_ok:
        try:
            text = _walk(msg, "", w, raw)
        except Exception as exc:  # a broken structure still keeps headers and the original
            defects.append(f"walk:{type(exc).__name__}")
    defects += w.defects
    _classify(w.attachments, w.html)

    plain = textclean.tidy(text.get("plain", ""))
    html = textclean.tidy(text.get("html", ""))
    if plain and (len(plain) >= 40 or not html):
        body_kind, body = "plain", plain
    elif html:
        body_kind, body = "html", html
    else:
        body_kind, body = "none", ""
    stripped = textclean.strip_quotes(body) if body else ""
    if w.forwarded:  # attached, not quoted: part of what this message says
        extra = "\n\n".join(w.forwarded)
        body = (body + "\n\n" + extra).strip()
        stripped = (stripped + "\n\n" + extra).strip()
        if body_kind == "none":
            body_kind = "plain"

    froms = _addresses(msg, "From")
    senders = _addresses(msg, "Sender")
    headers = {}
    for name, key in _RULE_HEADERS.items():
        v = _raw_header(msg, name)
        if v:
            headers[key] = v[:500]

    list_id = _raw_header(msg, "List-Id")
    reasons = []
    auto = (headers.get("auto_submitted") or "").lower()
    if auto and auto != "no":
        reasons.append("auto-submitted")
    if (headers.get("precedence") or "").lower() in ("bulk", "list", "junk"):
        reasons.append("precedence")
    if list_id or headers.get("list_unsubscribe"):
        reasons.append("list")
    if froms and _NOREPLY.match(froms[0].address.partition("@")[0]):
        reasons.append("noreply-sender")
    if headers.get("return_path", "").strip() == "<>":
        reasons.append("null-return-path")

    return ParsedMessage(
        message_id=_msgid(_raw_header(msg, "Message-ID")),
        in_reply_to=_msgid(_raw_header(msg, "In-Reply-To")),
        references=_msgids(_raw_header(msg, "References")),
        subject=_raw_header(msg, "Subject"),
        sent_at=_date(_raw_header(msg, "Date")),
        from_=froms[0] if froms else None,
        sender=senders[0] if senders else None,
        reply_to=_addresses(msg, "Reply-To"),
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        bcc=_addresses(msg, "Bcc"),
        list_id=list_id[:300] if list_id else None,
        headers=headers,
        is_automated=bool(reasons),
        automated_reasons=reasons,
        body_kind=body_kind,
        body_text=body,
        quote_stripped=stripped,
        snippet=textclean.snippet(stripped or body),
        size=len(raw),
        attachments=w.attachments,
        defects=defects,
    )


# ---------------------------------------------------------------- the HTML body, for display

def html_body(raw: bytes) -> str | None:
    """The message's own HTML, for the reading pane (talos.mailhtml cleans it): its text/html
    parts that are not files, joined in order; of a multipart/alternative only the last HTML
    version. A forwarded message (message/rfc822) is an attachment and is not walked. None when
    the message has no HTML or cannot be parsed. Nothing here is stored, so PARSER_VERSION is not
    concerned."""
    try:
        msg = BytesParser(policy=_POLICY).parsebytes(raw)
    except Exception:
        return None

    def walk(part: Message, level: int) -> list[str]:
        if level > MAX_DEPTH:
            return []
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            return []
        if part.is_multipart():
            found = [walk(child, level + 1) for child in _children(part)]
            if ctype == "multipart/alternative":
                last = [f for f in found if f]
                return last[-1] if last else []
            return [h for f in found for h in f]
        if ctype != "text/html":
            return []
        try:
            disposition = part.get_content_disposition()
            filename = part.get_filename()
        except Exception:
            return []
        if disposition == "attachment" or filename:
            return []
        return [_decode_text(part)]

    try:
        parts = walk(msg, 0)
    except Exception:
        return None
    html = "\n".join(p for p in parts if p and p.strip())
    return _nul_free(html) or None
