"""Desktop OAuth 2.0 flow with persistent token storage.

This is deliberately a *desktop* client flow, not a web one. The token is
minted on the user's machine and stored on the user's machine; no server
of ours ever sees it. That is what keeps this tool outside Google's
third-party-server rules for restricted scopes -- it is a design
constraint, not an implementation detail. Do not move this to a server.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..store.paths import find_credentials, token_path

SCOPES = [
    # Read, modify, and send. Notably cannot permanently delete -- trashing
    # is the most destructive thing this scope permits.
    "https://www.googleapis.com/auth/gmail.modify",
    # Only needed to create auto-trash filters for senders that ignore
    # unsubscribe requests. See --no-filters to run without it.
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

SCOPES_NO_FILTERS = ["https://www.googleapis.com/auth/gmail.modify"]


def _require_google_libs():
    """Lazy import so --help works before dependencies are installed."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        print(
            "\n[!] Google API libraries not installed.\n"
            "    Run:  pip install -e .\n",
            file=sys.stderr,
        )
        raise SystemExit(2) from e
    return Request, Credentials, InstalledAppFlow


def authenticate(scopes: list[str] | None = None):
    """Run the OAuth flow, reusing a stored refresh token when possible."""
    Request, Credentials, InstalledAppFlow = _require_google_libs()
    scopes = scopes or SCOPES

    creds = None
    token_file = token_path()

    # Honour a token left in the working directory by a pre-2.0 install.
    legacy_token = Path.cwd() / "token.json"
    if not token_file.exists() and legacy_token.exists():
        try:
            token_file.write_text(legacy_token.read_text(encoding="utf-8"), encoding="utf-8")
            token_file.chmod(0o600)
            print(f"  • Migrated existing login to {token_file.parent}")
        except OSError:
            token_file = legacy_token

    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes)
        except (ValueError, OSError) as e:
            print(f"  ! Could not load existing token: {e}", file=sys.stderr)
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"  ! Token refresh failed, re-authenticating: {e}", file=sys.stderr)
            creds = None

    if not creds or not creds.valid:
        creds_path = find_credentials()
        if creds_path is None:
            print(
                "\n[!] credentials.json not found.\n"
                "    See README.md for the one-time Google Cloud setup.\n"
                "    Save your Desktop-app OAuth JSON as credentials.json in:\n"
                f"    {token_file.parent}\n",
                file=sys.stderr,
            )
            sys.exit(2)

        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), scopes)
        creds = flow.run_local_server(port=0)

        _save_token(token_file, creds)

    return creds


def _save_token(path: Path, creds) -> None:
    """Persist the refresh token, readable only by this user.

    Default file permissions would leave a live Gmail credential
    world-readable on a shared machine.
    """
    try:
        path.write_text(creds.to_json(), encoding="utf-8")
        path.chmod(0o600)
    except OSError as e:
        print(f"  ! Could not save token: {e}", file=sys.stderr)


def build_gmail_service(creds):
    """Authenticated Gmail discovery client, for the mutation calls."""
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def logout() -> bool:
    """Delete the stored token. The account stays connected in Google's
    settings until the user revokes it there; say so in the UI."""
    path = token_path()
    if path.exists():
        path.unlink()
        return True
    return False
