"""Google OAuth 2.0 authentication flow with persistent token storage."""
from __future__ import annotations

import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


def _require_google_libs():
    """Lazy import to allow --help to work without dependencies installed."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        print(
            "\n[!] Google API libraries not installed.\n"
            "    Run:  pip install -r requirements.txt\n",
            file=sys.stderr,
        )
        raise SystemExit(2) from e
    return Request, Credentials, InstalledAppFlow


def authenticate():  # -> Credentials
    """Run the OAuth flow, reusing a stored refresh token when possible.

    Raises SystemExit with a friendly message if credentials.json is missing
    or if the Google libraries are not installed.
    """
    Request, Credentials, InstalledAppFlow = _require_google_libs()

    creds = None
    token_path = Path(TOKEN_FILE)

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, OSError) as e:
            print(f"  ! Could not load existing token: {e}", file=sys.stderr)
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"  ! Token refresh failed: {e}", file=sys.stderr)
            creds = None

    if not creds or not creds.valid:
        creds_path = _find_credentials_file()
        if creds_path is None:
            print(
                "\n[!] credentials.json not found.\n"
                "    See README.md for the one-time Google Cloud setup.\n"
                "    Download your Desktop-app OAuth JSON and save it as:\n"
                f"    {CREDENTIALS_FILE}\n"
                "    (a file matching client_secret_*.json in this directory also works)\n",
                file=sys.stderr,
            )
            sys.exit(2)

        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = flow.run_local_server(port=0)

        try:
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except OSError as e:
            print(f"  ! Could not save token: {e}", file=sys.stderr)

    return creds


def build_gmail_service(creds):
    """Build an authenticated Gmail API client."""
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _find_credentials_file() -> Path | None:
    """Look for credentials.json, or auto-detect a client_secret_*.json file."""
    p = Path(CREDENTIALS_FILE)
    if p.exists():
        return p
    matches = sorted(Path(".").glob("client_secret_*.json"))
    if matches:
        print(f"  • Auto-detected credentials: {matches[0].name}", file=sys.stderr)
        return matches[0]
    return None
