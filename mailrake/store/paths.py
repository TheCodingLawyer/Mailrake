"""Where this tool keeps its state on each platform.

Everything lives in one directory so a user can inspect, back up, or delete
the tool's entire footprint in a single move. That matters for a local-first
tool holding a Gmail token: "where is my data" must have a one-line answer.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "mailrake"
LEGACY_APP_DIR_NAME = "gmail-unsub"  # pre-rename; migrated on first run


def config_dir() -> Path:
    """The per-user state directory, created on demand."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")

    path = Path(base) / APP_DIR_NAME
    if not path.exists():
        _adopt_legacy_dir(Path(base) / LEGACY_APP_DIR_NAME, path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _adopt_legacy_dir(old: Path, new: Path) -> None:
    """Carry state over from the pre-rename directory.

    A rename must not cost the user their login, their trusted senders or
    their action history. Copy rather than move, so a downgrade still works.
    """
    if not old.is_dir():
        return
    try:
        new.mkdir(parents=True, exist_ok=True)
        for item in old.iterdir():
            if item.is_file() and not (new / item.name).exists():
                (new / item.name).write_bytes(item.read_bytes())
        print(f"  • Adopted existing state from {old.name}/ (rename to {new.name})")
    except OSError:
        pass


def db_path() -> Path:
    return config_dir() / "state.db"


def token_path() -> Path:
    return config_dir() / "token.json"


def find_credentials() -> Path | None:
    """Locate the OAuth client secrets file.

    Checks the config dir first, then the working directory, so the pre-2.0
    layout (credentials.json beside the source) keeps working untouched.
    """
    names = ["credentials.json"]
    for directory in (config_dir(), Path.cwd()):
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return candidate
        matches = sorted(directory.glob("client_secret_*.json"))
        if matches:
            return matches[0]
    return None


def legacy_files() -> dict[str, Path]:
    """Pre-2.0 state files in the working directory, for one-time import."""
    cwd = Path.cwd()
    return {
        "config": cwd / "config.json",
        "failed": cwd / "failed-unsubs.json",
        "token": cwd / "token.json",
    }
