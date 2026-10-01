from datetime import timezone

import mailfactory as mf

from talos import mime, textclean


def test_a_plain_message_yields_its_people_subject_and_date():
    p = mime.parse(mf.make(cc="Mikael <mikael@nordvik.se>", subject="Brandväggsfönster fredag?"))
    assert p.from_.address == "oskar@nordvik.se"
    assert p.from_.name == "Oskar Nyström"
    assert [a.address for a in p.to] == ["owner@gmail.com"]
    assert [a.address for a in p.cc] == ["mikael@nordvik.se"]
    assert p.subject == "Brandväggsfönster fredag?"
    assert p.sent_at.tzinfo is not None and p.sent_at.astimezone(timezone.utc).hour == 8
    assert p.body_kind == "plain" and "Kan du titta" in p.body_text


def test_an_html_only_message_is_turned_into_readable_text():
    html = "<html><head><style>p{}</style></head><body><p>Din faktura</p><p>Belopp: 450 kr</p>" \
           "<script>track()</script></body></html>"
    p = mime.parse(mf.make(body=None, html=html))
    assert p.body_kind == "html"
    assert "Din faktura" in p.body_text and "450 kr" in p.body_text
    assert "track()" not in p.body_text and "p{}" not in p.body_text


def test_a_swedish_reply_keeps_only_the_new_text():
    body = ("Ja, fredag 18–20 fungerar.\n\nDen tis 22 sep. 2026 kl 08:14 skrev Oskar Nyström <oskar@nordvik.se>:\n"
            "> Kan du bekräfta fredag?\n")
    p = mime.parse(mf.make(body=body))
    assert p.quote_stripped == "Ja, fredag 18–20 fungerar."


def test_an_outlook_reply_block_is_cut_in_either_language():
    body = "Tack!\n\nFrån: Oskar Nyström\nSkickat: den 22 september 2026 08:14\nTill: Alex\nÄmne: Hej\n\nGammalt"
    assert textclean.strip_quotes(body) == "Tack!"
    body_en = "Thanks!\n\nFrom: Oskar\nSent: Tuesday\nTo: Alex\n\nOld text"
    assert textclean.strip_quotes(body_en) == "Thanks!"


def test_encoded_headers_are_decoded():
    raw = (b"From: =?utf-8?Q?F=C3=B6rs=C3=A4kringskassan?= <noreply@fk.se>\r\n"
           b"To: owner@gmail.com\r\nSubject: =?utf-8?B?RMOkciDDpHIgZGluIGJyZXY=?=\r\n"
           b"Date: Mon, 21 Sep 2026 10:00:00 +0200\r\nMessage-ID: <a@fk.se>\r\n\r\nText\r\n")
    p = mime.parse(raw)
    assert p.from_.name == "Försäkringskassan"
    assert p.subject == "Där är din brev"


def test_attachments_are_found_with_their_names_and_bytes():
    pdf = mf.pdf("Faktura 1234")
    p = mime.parse(mf.make(attachments=[("faktura.pdf", "application/pdf", pdf),
                                        ("bild.jpg", "image/jpeg", mf.jpeg())]))
    names = {a.filename: a for a in p.attachments}
    assert set(names) == {"faktura.pdf", "bild.jpg"}
    assert names["faktura.pdf"].data == pdf


def test_machine_mail_is_recognised_by_its_headers_and_sender():
    p = mime.parse(mf.make(frm="Backup <noreply@veeam.example>", headers={"Auto-Submitted": "auto-generated"}))
    assert p.is_automated
    assert set(p.automated_reasons) >= {"auto-submitted", "noreply-sender"}
    assert not mime.parse(mf.make()).is_automated


def test_a_broken_date_does_not_stop_parsing():
    raw = mf.make().replace(b"Date: ", b"Date: not a date at all ")
    p = mime.parse(raw)
    assert p.sent_at is None
    assert p.subject == "Hej"


def test_subject_prefixes_in_several_languages_are_removed_for_threading():
    assert textclean.normalize_subject("SV: VB: Re: Offert") == "offert"
    assert textclean.normalize_subject("AW: Fwd: Meeting") == "meeting"


# ---------------------------------------------------------------- parser version 2


def test_text_after_an_inline_photo_is_kept():
    body = f"""--B
Content-Type: text/plain; charset=utf-8

Här är första bilden från bygget.
--B
Content-Type: image/jpeg; name="IMG_0001.jpg"
Content-Disposition: inline; filename="IMG_0001.jpg"
Content-Transfer-Encoding: base64

{mf.JPEG_B64}
--B
Content-Type: text/plain; charset=utf-8

Och taket ser bra ut nu, efter regnet.
--B--
"""
    p = mime.parse(mf.raw('multipart/mixed; boundary="B"', body))
    assert "första bilden" in p.body_text and "taket ser bra ut" in p.body_text
    assert p.body_text.index("första bilden") < p.body_text.index("taket")
    assert [(a.filename, a.is_attachment) for a in p.attachments] == [("IMG_0001.jpg", True)]


def test_html_split_around_an_image_is_joined():
    body = f"""--B
Content-Type: text/html; charset=utf-8

<html><body><p>Första delen av brevet.</p></body></html>
--B
Content-Type: image/png; name="bild.png"
Content-Disposition: inline; filename="bild.png"
Content-Transfer-Encoding: base64

{mf.JPEG_B64}
--B
Content-Type: text/html; charset=utf-8

<html><body><p>Andra delen, efter bilden.</p></body></html>
--B--
"""
    p = mime.parse(mf.raw('multipart/mixed; boundary="B"', body))
    assert p.body_kind == "html"
    assert "Första delen" in p.body_text and "Andra delen" in p.body_text


def test_plain_and_html_versions_of_the_same_text_are_not_counted_twice():
    body = f"""--M
Content-Type: multipart/alternative; boundary="A1"

--A1
Content-Type: text/plain; charset=utf-8

Första stycket, med tillräckligt mycket text för att räcka.
--A1
Content-Type: text/html; charset=utf-8

<p>Första stycket, med tillräckligt mycket text för att räcka.</p>
--A1--
--M
Content-Type: image/jpeg; name="IMG_0002.jpg"
Content-Disposition: inline; filename="IMG_0002.jpg"
Content-Transfer-Encoding: base64

{mf.JPEG_B64}
--M
Content-Type: multipart/alternative; boundary="A2"

--A2
Content-Type: text/plain; charset=utf-8

Andra stycket efter bilden.
--A2
Content-Type: text/html; charset=utf-8

<p>Andra stycket efter bilden.</p>
--A2--
--M--
"""
    p = mime.parse(mf.raw('multipart/mixed; boundary="M"', body))
    assert p.body_kind == "plain"
    assert p.body_text.count("Första stycket") == 1 and p.body_text.count("Andra stycket") == 1


def test_a_part_is_an_attachment_only_if_the_sender_attached_it():
    invite = mime.parse(mf.raw('multipart/alternative; boundary="A"', mf.INVITE))
    assert [(a.content_type, a.is_attachment) for a in invite.attachments] == [("text/calendar", False)]
    logo = mime.parse(mf.raw('multipart/related; boundary="R"', mf.LOGO))
    assert [(a.filename, a.is_attachment) for a in logo.attachments] == [("logo.png", False)]
    pdf = mime.parse(mf.raw('multipart/mixed; boundary="B"', mf.INLINE_PDF))  # Apple Mail sends PDFs inline
    assert [(a.filename, a.is_attachment) for a in pdf.attachments] == [("faktura.pdf", True)]
    unreferenced = mf.LOGO.replace("cid:logo@nordvik.se", "https://nordvik.se/logo.png")
    assert mime.parse(mf.raw('multipart/related; boundary="R"', unreferenced)).attachments[0].is_attachment


def test_swedish_letters_survive_8_bit_mail_without_a_charset():
    latin1 = ("From: Jörgen Åström <jorgen@nordvik.se>\r\nTo: owner@gmail.com\r\n"
              "Subject: Räkning från oss\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
              "--B\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: 8bit\r\n\r\nHär är din räkning.\r\n"
              "--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"Räkning.pdf\"\r\n"
              "\r\n%PDF-1.4\r\n--B--\r\n").encode("latin-1")
    p = mime.parse(latin1)
    assert p.subject == "Räkning från oss"
    assert p.from_.name == "Jörgen Åström"
    assert p.body_text == "Här är din räkning."
    assert p.attachments[0].filename == "Räkning.pdf"
    utf8 = (b"From: a@nordvik.se\r\nSubject: Hej\r\nContent-Type: text/plain\r\n\r\n" + "Hälsningar från Uppsala".encode())
    assert mime.parse(utf8).body_text == "Hälsningar från Uppsala"
    assert mime.parse(latin1.replace(b"Content-Type: text/plain\r\n", b"Content-Type: text/plain; charset=us-ascii\r\n")
                      ).body_text == "Här är din räkning."


def test_a_lone_surrogate_from_utf_7_becomes_a_replacement_character():
    p = mime.parse(mf.raw("text/plain; charset=utf-7", "Hej +2AA- d+AOU-"))
    assert p.body_text == "Hej � då"
    p.body_text.encode("utf-8")  # what psycopg must do


def test_a_forwarded_message_is_stored_as_its_original_bytes():
    p = mime.parse(mf.forwarding(mf.INNER))
    first, second = p.attachments
    assert first.content_type == second.content_type == "message/rfc822"
    assert first.data == mf.INNER  # base64 decoded, not stored as base64 text
    assert second.data == mf.INNER.replace(b"Offerten", b"Andra offerten")  # CRLF and 8-bit kept as they were
    assert first.filename == "Offert.eml" and first.is_attachment


def test_the_text_of_a_forwarded_message_is_part_of_the_body():
    p = mime.parse(mf.forwarding(mf.INNER))
    assert p.body_text.startswith("Se nedan.")
    assert "Offerten gäller till fredag." in p.body_text and "Andra offerten gäller" in p.body_text
    assert "Offerten gäller" in p.quote_stripped  # attached, not quoted: it is searched
    assert "[Forwarded message: Offert, from Mikael]" in p.body_text


def test_absurdly_deep_nesting_keeps_the_headers_and_does_not_raise():
    p = mime.parse(mf.nested(1000))
    assert p.subject == "Djupt" and p.from_.address == "oskar@nordvik.se"
    assert "parse:RecursionError" in p.defects
    moderate = mime.parse(mf.nested(60))
    assert moderate.subject == "Djupt" and "walk:too-deep" in moderate.defects


def test_a_nul_in_html_does_not_corrupt_the_text_next_to_it():
    assert textclean.html_to_text("<p>a\x00b</p>") == "ab"


def test_an_encrypted_pdf_is_marked_encrypted_not_error():
    from talos import extract
    locked = extract.extract(mf.encrypted_pdf("Hemligt", "lösenord"), "application/pdf", "lön.pdf")
    assert locked.status == "encrypted" and locked.attrs["encrypted"]
    owner_only = extract.extract(mf.encrypted_pdf("Öppet", ""), "application/pdf", "info.pdf")
    assert owner_only.status == "ok" and "Öppet" in owner_only.text
