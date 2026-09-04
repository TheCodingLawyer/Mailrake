"""Hardening for the local server.

A localhost server holding a Gmail token that can trash a mailbox is a real
attack surface. Any page the user has open can attempt requests to
127.0.0.1, and DNS rebinding can make a hostile domain resolve there. Two
defences, both required:

  * a per-session token, generated at launch and handed over only in the URL
    we open ourselves, so a blind cross-origin request cannot guess it;
  * strict Origin/Host checks, so a rebound domain is rejected even if the
    token somehow leaked.

Browsers send Origin on every cross-origin request and cannot forge it from
page JavaScript, which is what makes the second check meaningful.
"""
from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlparse

from fastapi import HTTPException, Request

SESSION_TOKEN = secrets.token_urlsafe(32)

# Only loopback. Not "localhost" by name -- a hostile DNS record can point a
# name at 127.0.0.1, which is exactly the rebinding attack we are blocking.
ALLOWED_HOSTNAMES = {"127.0.0.1", "[::1]", "localhost"}


def _host_is_loopback(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname.strip("[]")).is_loopback
    except ValueError:
        return False


def check_origin(request: Request) -> None:
    """Reject anything not originating from our own loopback page."""
    host_header = request.headers.get("host", "")
    hostname = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
    if not _host_is_loopback(hostname):
        raise HTTPException(403, "Invalid Host header")

    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if not parsed.hostname or not _host_is_loopback(parsed.hostname):
            raise HTTPException(403, "Cross-origin request refused")


def check_token(request: Request) -> None:
    """Constant-time check of the per-session token."""
    supplied = request.headers.get("x-session-token") or \
        request.query_params.get("token", "")
    if not secrets.compare_digest(supplied, SESSION_TOKEN):
        raise HTTPException(401, "Missing or invalid session token")


async def guard(request: Request) -> None:
    """FastAPI dependency applied to every API route."""
    check_origin(request)
    check_token(request)
