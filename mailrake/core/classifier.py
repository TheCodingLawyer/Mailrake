"""Sensitive-sender classifier: the guard against unsubscribing from things
that matter.

Matching is on word boundaries so a keyword cannot fire on an unrelated
substring ("gov" must not match "Governors Ball"). Common English
inflections are accepted, because real sender addresses are overwhelmingly
plural -- `accounts@`, `receipts@`, `statements@`, `payments@` -- and a
strict boundary silently missed every one of them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import List

# Suffixes allowed immediately after a keyword. Deliberately short: it is the
# difference between "account"/"accounts" and "tax"/"taxes", not a stemmer.
INFLECTIONS = "s|es|ed|ing"


@dataclass
class SensitivityCheck:
    is_sensitive: bool
    reasons: List[str]


@lru_cache(maxsize=512)
def _pattern(keyword: str) -> re.Pattern[str]:
    return re.compile(
        r"(?:^|[^a-z0-9])" + re.escape(keyword) + rf"(?:{INFLECTIONS})?(?:[^a-z0-9]|$)"
    )


def _haystack(email: str, display_name: str) -> str:
    text = f"{display_name or ''} {email or ''}".lower()
    return re.sub(r"[^a-z0-9.@\-_ ]+", " ", text)


def check_sender(
    email: str,
    display_name: str,
    keywords: List[str],
) -> SensitivityCheck:
    """Return whether a sender looks sensitive, and which keywords matched."""
    if not keywords:
        return SensitivityCheck(False, [])

    text = _haystack(email, display_name)

    matched: List[str] = []
    for kw in keywords:
        kw_l = (kw or "").lower().strip()
        if kw_l and _pattern(kw_l).search(text):
            matched.append(kw_l)

    return SensitivityCheck(bool(matched), matched)
