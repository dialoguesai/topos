"""F1.6 identifier guard (A1.0): deterministic recognizers as a REJECTING validator.

A rule the extractor merely reads is not a guarantee — this runs on every candidate
fact value string, and a hit means the assertion is dropped and counted, even if the
model emitted it. Rules tier only: no model weights, no offset contract, no vault.
"""
from __future__ import annotations

import re
from typing import List

_PATTERNS = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?<!\d)(?:\+?\d[\s().-]?){9,14}\d(?!\d)")),
    ("card_like", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")),
    ("ssn_like", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    ("iban_like", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")),
    ("long_id_run", re.compile(r"(?<![\dA-Za-z])[A-Z0-9]{9,}(?![\dA-Za-z])")),  # policy/case/document numbers
    ("secret_like", re.compile(r"\b(?:sk|pk|key|tok|ghp|xox)[-_][A-Za-z0-9_-]{12,}\b", re.I)),
    ("url_credential", re.compile(r"[?&](?:token|key|code|otp|password)=[^&\s]+", re.I)),
]
# Values that legitimately look numeric but are fine (years, small counts, dates).
_DATE_OK = re.compile(r"^\d{4}(-\d{2}){0,2}$")


def _luhn_ok(digits: str) -> bool:
    ds = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(ds) <= 19:
        return False
    total, alt = 0, False
    for d in reversed(ds):
        d = d * 2 - 9 if alt and d * 2 > 9 else (d * 2 if alt else d)
        total += d
        alt = not alt
    return total % 10 == 0


def find_identifiers(value: str) -> List[str]:
    """Return the kinds of identifier found in a candidate fact value ([] = clean)."""
    text = str(value or "")
    if not text or _DATE_OK.match(text.strip()):
        return []
    hits: List[str] = []
    for kind, pat in _PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        if kind == "card_like" and not _luhn_ok(m.group(0)):
            continue  # 13-19 digit runs that fail Luhn are caught by long_id_run if truly id-like
        hits.append(kind)
    return hits
