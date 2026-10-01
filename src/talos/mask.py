"""Masking before anything leaves the Mac (docs/enrichment-plan.md §10).

`mask(text)` replaces, in this order:

1. **URL paths and queries**, keeping the scheme and host: `https://portal.example.se/a/b?t=1`
   becomes `https://portal.example.se/…`. A bare `www.` host is treated the same.
2. **IBANs**, checked with ISO 13616 mod 97: `SE45 5000 0000 0583 9825 7466` → `[iban]`.
3. **Card numbers**: 13–19 digits, optionally in groups split by spaces or dashes, that pass the
   Luhn check → `[card]`.
4. **Swedish personal ID numbers** (personnummer and samordningsnummer): (YY)YYMMDD, then `-`,
   `+` or nothing, then four digits, with a valid month and day (day + 60 for a
   samordningsnummer). With a separator any such number is masked; without one it must also
   pass the Luhn check, so a bare order number is left alone → `[personnummer]`.
5. **Phone numbers**: international (`+46 70 123 45 67`, `0046…`) or Swedish domestic (`070-123 45
   67`, `031-12 34 56`, `08-123 456 78`), 7 to 15 digits in all → `[phone]`. A date, a time or
   an amount is not a phone number: a candidate glued to a digit, a dash, a dot or a slash on
   its left is skipped.

Pure functions; no database, no network. Bump VERSION when the output changes, so a run's
records say which masking they had.
"""

from __future__ import annotations

import re

VERSION = 1

_URL = re.compile(r"(?i)\b((?:https?|ftp)://|www\.)([a-z0-9.-]+(?::\d+)?)([/?#][^\s<>\"')\]]*)?")
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30})\b")
_CARD = re.compile(r"(?<![\d-])(\d(?:[ -]?\d){12,18})(?![\d-])")
_PNR = re.compile(r"(?<![\d-])((?:19|20)?\d{2})(\d{2})(\d{2})([-+]?)(\d{4})(?![\d-])")
_PHONE = re.compile(r"(?<![\w\-./:+])((?:\+|00)\d{1,3}[ -]?(?:\(0\)[ -]?)?\d{1,4}(?:[ -]?\d{2,4}){1,4}"
                    r"|0\d{1,3}[ /-]?\d{2,4}(?:[ -]?\d{2,4}){0,3})(?![\w\-./:])")


def luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1
    except ValueError:
        return False


def _url(m: re.Match) -> str:
    scheme, host, rest = m.group(1), m.group(2), m.group(3)
    return f"{scheme}{host}/…" if rest and rest not in ("/",) else m.group(0)


def _iban(m: re.Match) -> str:
    return "[iban]" if _iban_ok(m.group(1)) else m.group(0)


def _card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(1))
    return "[card]" if 13 <= len(digits) <= 19 and luhn(digits) else m.group(0)


def _pnr(m: re.Match) -> str:
    year, month, day, sep, tail = m.groups()
    mm, dd = int(month), int(day)
    if not 1 <= mm <= 12 or not (1 <= dd <= 31 or 61 <= dd <= 91):
        return m.group(0)
    if not sep and not luhn(year[-2:] + month + day + tail):
        return m.group(0)
    return "[personnummer]"


def _phone(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(1))
    return "[phone]" if 7 <= len(digits) <= 15 else m.group(0)


def mask(text: str | None) -> str:
    """The text with URL paths, IBANs, card numbers, personal ID numbers and phone numbers masked."""
    if not text:
        return text or ""
    text = _URL.sub(_url, text)
    text = _IBAN.sub(_iban, text)
    text = _CARD.sub(_card, text)
    text = _PNR.sub(_pnr, text)
    return _PHONE.sub(_phone, text)
