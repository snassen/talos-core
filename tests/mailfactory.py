"""Invented test mail. Nothing here comes from a real mailbox.

make() builds a complete RFC 822 message; the helpers build small but real
attachments (a PDF with text, an Excel file, a JPEG with EXIF) so extraction is
tested on actual file formats rather than on stubs.
"""

from __future__ import annotations

import base64
import io
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone

ME = "owner@gmail.com"


def make(*, frm="Oskar Nyström <oskar@nordvik.se>", to=ME, cc=None, subject="Hej",
         body="Hej Alex,\n\nKan du titta på detta?\n\nMvh Oskar", html=None,
         date=datetime(2026, 9, 22, 8, 14, tzinfo=timezone.utc), msgid=None, in_reply_to=None,
         references=None, attachments=(), headers=None) -> bytes:
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Subject"] = subject
    m["Date"] = format_datetime(date)
    m["Message-ID"] = msgid or f"<{abs(hash((frm, subject, date)))}@test.invalid>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = references
    for k, v in (headers or {}).items():
        m[k] = v
    if body is not None:
        m.set_content(body)
    if html is not None:
        if body is None:
            m.set_content(html, subtype="html")
        else:
            m.add_alternative(html, subtype="html")
    for filename, ctype, data in attachments:
        maintype, subtype = ctype.split("/", 1)
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return m.as_bytes()


def pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def xlsx(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Kvitton"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def jpeg() -> bytes:
    from PIL import Image

    im = Image.new("RGB", (32, 24), (200, 120, 40))
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x0110] = "iPhone 15"
    exif[0x0132] = "2026:09:01 12:00:00"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def encrypted_pdf(text: str, user_password: str, owner_password: str = "ägare") -> bytes:
    """A PDF encrypted with RC4 (no extra libraries). An empty user password opens it."""
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf(text))))
    w.encrypt(user_password=user_password, owner_password=owner_password, algorithm="RC4-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def raw(content_type: str, body: str | bytes, *, subject: str = "Hej") -> bytes:
    """A hand-built message, for MIME structures EmailMessage will not produce (or fixes).
    Line endings are CRLF, as on the wire."""
    head = (f"From: Oskar Nyström <oskar@nordvik.se>\nTo: {ME}\nSubject: {subject}\n"
            f"Date: Tue, 22 Sep 2026 08:14:00 +0000\nMessage-ID: <{abs(hash((content_type, subject)))}@test.invalid>\n"
            f"MIME-Version: 1.0\nContent-Type: {content_type}\n\n")
    data = body if isinstance(body, bytes) else body.encode("utf-8")
    return head.replace("\n", "\r\n").encode("utf-8") + data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


# ---------------------------------------------------------------- MIME structures from real clients
# Bodies for raw(); each copies the shape (not the content) of what a real client sends.

JPEG_B64 = base64.encodebytes(jpeg()).decode("ascii")

# An invitation: the calendar is an alternative to the text, not a file.
INVITE = """--A
Content-Type: text/plain; charset=utf-8

Möte om brandväggen på fredag.
--A
Content-Type: text/calendar; charset=utf-8; method=REQUEST

BEGIN:VCALENDAR
METHOD:REQUEST
END:VCALENDAR
--A--
"""

# A newsletter whose logo the HTML shows by cid:.
LOGO = f"""--R
Content-Type: text/html; charset=utf-8

<html><body><img src="cid:logo@nordvik.se"><p>Nyhetsbrev från Nordvik, september.</p></body></html>
--R
Content-Type: image/png; name="logo.png"
Content-Disposition: inline; filename="logo.png"
Content-ID: <logo@nordvik.se>
Content-Transfer-Encoding: base64

{JPEG_B64}
--R--
"""

# Apple Mail: a PDF sent inline, with a Content-ID, is still an attachment.
INLINE_PDF = f"""--B
Content-Type: text/plain; charset=utf-8

Fakturan bifogad.
--B
Content-Type: application/pdf; name="faktura.pdf"
Content-Disposition: inline; filename="faktura.pdf"
Content-ID: <3F2A@apple.example>
Content-Transfer-Encoding: base64

{base64.encodebytes(pdf("Faktura 77")).decode("ascii")}
--B--
"""


def forwarding(inner: bytes) -> bytes:
    """A forward carrying `inner` twice: base64-encoded (as some clients do) and as raw 8-bit."""
    body = (b"--F\nContent-Type: text/plain; charset=utf-8\n\nSe nedan.\n"
            b"--F\nContent-Type: message/rfc822\nContent-Transfer-Encoding: base64\n\n"
            + base64.encodebytes(inner)
            + b"--F\nContent-Type: message/rfc822\nContent-Disposition: attachment\n\n"
            + inner.replace(b"Offerten", b"Andra offerten")
            + b"\n--F--\n")
    return raw('multipart/mixed; boundary="F"', body, subject="VB: Offert")


# The forwarded original: CRLF line ends and 8-bit UTF-8, which re-serialising would change.
INNER = (b"From: Mikael <mikael@nordvik.se>\r\nTo: oskar@nordvik.se\r\nSubject: Offert\r\n"
         b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
         + "Offerten gäller till fredag.\r\n".encode())


def nested(depth: int) -> bytes:
    """multipart/mixed inside multipart/mixed, `depth` levels down, with one text part at the bottom."""
    body = b""
    for i in range(depth):
        body += b"--b%d\nContent-Type: multipart/mixed; boundary=b%d\n\n" % (i, i + 1)
    body += b"--b%d\nContent-Type: text/plain\n\ndeep inside\n--b%d--\n" % (depth, depth)
    for i in range(depth - 1, -1, -1):
        body += b"--b%d--\n" % i
    return raw("multipart/mixed; boundary=b0", body, subject="Djupt")
