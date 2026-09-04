"""Unsubscribe via HTTPS one-click (RFC 8058) or mailto."""
from __future__ import annotations

import base64
import re
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests

USER_AGENT = "Gmail-Unsub/1.0 (+https://github.com/local/gmail-unsub)"
HTTP_TIMEOUT = 15


@dataclass
class UnsubscribeTarget:
    method: str  # "https" or "mailto"
    value: str   # url or email address


@dataclass
class UnsubscribeResult:
    ok: bool
    method: str
    detail: str


def parse_list_unsubscribe(header: str) -> List[UnsubscribeTarget]:
    """Parse a List-Unsubscribe header into actionable targets.

    Per RFC 8058, header looks like:
        List-Unsubscribe: <https://example.com/u>, <mailto:u@example.com>
    The companion header List-Unsubscribe-Post: List-Unsubscribe=One-Click
    indicates one-click POST support.
    """
    targets: List[UnsubscribeTarget] = []
    if not header:
        return targets

    for m in re.finditer(r"<([^>]+)>", header):
        raw = m.group(1).strip()
        if raw.lower().startswith("https://") or raw.lower().startswith("http://"):
            targets.append(UnsubscribeTarget("https", raw))
        elif raw.lower().startswith("mailto:"):
            addr = raw[len("mailto:"):].split("?")[0]
            addr = unquote(addr).strip()
            if addr:
                targets.append(UnsubscribeTarget("mailto", addr))

    return targets


def choose_target(
    targets: List[UnsubscribeTarget], has_one_click_post: bool
) -> Optional[UnsubscribeTarget]:
    """Prefer HTTPS one-click, then HTTPS, then mailto."""
    if not targets:
        return None
    https = [t for t in targets if t.method == "https"]
    if https and has_one_click_post:
        return https[0]
    if https:
        return https[0]
    mailtos = [t for t in targets if t.method == "mailto"]
    if mailtos:
        return mailtos[0]
    return None


def https_unsubscribe(url: str, one_click: bool) -> UnsubscribeResult:
    """Send an unsubscribe request to an HTTPS endpoint.

    Per RFC 8058, a List-Unsubscribe-Post: List-Unsubscribe=One-Click header
    indicates the server supports a one-click POST. Otherwise we send a GET
    which is a graceful fallback.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http"):
            return UnsubscribeResult(False, "https", f"bad scheme: {parsed.scheme}")
    except ValueError as e:
        return UnsubscribeResult(False, "https", f"parse error: {e}")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        if one_click:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            resp = requests.post(
                url,
                data="List-Unsubscribe=One-Click",
                headers=headers,
                timeout=HTTP_TIMEOUT,
                allow_redirects=True,
            )
        else:
            resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        return UnsubscribeResult(False, "https", f"request failed: {e}")

    if 200 <= resp.status_code < 400:
        verb = "POST" if one_click else "GET"
        return UnsubscribeResult(True, "https", f"{verb} {url} -> {resp.status_code}")
    return UnsubscribeResult(False, "https", f"HTTP {resp.status_code}")


def mailto_unsubscribe(service, address: str, original_subject: str = "") -> UnsubscribeResult:
    """Send an unsubscribe email via Gmail API."""
    try:
        from googleapiclient.errors import HttpError  # lazy import
        msg = EmailMessage()
        msg["To"] = address
        msg["From"] = "me"
        msg["Subject"] = "unsubscribe"
        msg.set_content(
            "Please unsubscribe this address from your mailing list.\n\n"
            f"Reference: {original_subject or '(no subject)'}\n"
        )

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return UnsubscribeResult(True, "mailto", f"sent unsubscribe to {address}")
    except HttpError as e:
        return UnsubscribeResult(False, "mailto", f"Gmail API error: {e}")
    except Exception as e:
        return UnsubscribeResult(False, "mailto", f"send error: {e}")



def execute_unsubscribe(
    service,
    targets: List[UnsubscribeTarget],
    has_one_click_post: bool,
    original_subject: str = "",
) -> UnsubscribeResult:
    """Pick the best target and execute it."""
    target = choose_target(targets, has_one_click_post)
    if target is None:
        return UnsubscribeResult(False, "none", "no actionable unsubscribe target")

    if target.method == "https":
        return https_unsubscribe(target.value, one_click=has_one_click_post)
    if target.method == "mailto":
        return mailto_unsubscribe(service, target.value, original_subject)

    return UnsubscribeResult(False, "none", f"unknown method: {target.method}")


def describe_targets(targets: List[UnsubscribeTarget], has_one_click_post: bool) -> str:
    """Human-readable description of available methods."""
    if not targets:
        return "none"
    chosen = choose_target(targets, has_one_click_post)
    if chosen is None:
        return "none"
    if chosen.method == "https":
        verb = "HTTPS one-click POST" if has_one_click_post else "HTTPS"
        return f"{verb}"
    return f"mailto: {chosen.value}"


def retry_unsubscribe(
    service,
    targets: List[UnsubscribeTarget],
    has_one_click_post: bool,
    failed_method: str,
    original_subject: str = "",
) -> UnsubscribeResult:
    """Try every available method EXCEPT the one that already failed."""
    https_targets = [t for t in targets if t.method == "https"]
    mailto_targets = [t for t in targets if t.method == "mailto"]

    attempts = []
    if has_one_click_post and https_targets:
        attempts.append(("https_post", https_targets[0].value, "https one-click POST"))
    if https_targets:
        attempts.append(("https_get", https_targets[0].value, "HTTPS GET"))
    if mailto_targets:
        attempts.append(("mailto", mailto_targets[0].value, "mailto"))

    for mname, value, label in attempts:
        is_same = (
            failed_method in ("https", "https_post", "https_get")
            and mname.startswith("https")
        ) or failed_method == mname
        if is_same:
            continue

        if mname == "https_post":
            result = https_unsubscribe(value, one_click=True)
        elif mname == "https_get":
            result = https_unsubscribe(value, one_click=False)
        elif mname == "mailto":
            result = mailto_unsubscribe(service, value, original_subject)
        else:
            continue

        if result.ok:
            result.detail = f"(retry {label}) {result.detail}"
            return result

    return UnsubscribeResult(False, "retry", "all available methods exhausted")
