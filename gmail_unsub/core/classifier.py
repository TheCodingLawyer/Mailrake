"""Sensitive-sender classifier to prevent accidental unsubscribes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class SensitivityCheck:
    is_sensitive: bool
    reasons: List[str]


def _haystack(email: str, display_name: str) -> str:
    return f"{display_name or ''} {email or ''}".lower()


def check_sender(
    email: str,
    display_name: str,
    keywords: List[str],
) -> SensitivityCheck:
    """Return whether a sender looks sensitive and which keywords matched."""
    if not keywords:
        return SensitivityCheck(False, [])

    text = _haystack(email, display_name)
    text = re.sub(r"[^a-z0-9.@\-_ ]+", " ", text)

    matched: List[str] = []
    for kw in keywords:
        kw_l = (kw or "").lower().strip()
        if not kw_l:
            continue
        if re.search(r"(?:^|[^a-z0-9])" + re.escape(kw_l) + r"(?:[^a-z0-9]|$)", text):
            matched.append(kw_l)

    return SensitivityCheck(bool(matched), matched)
