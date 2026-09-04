"""Local SQLite state.

Replaces the pre-2.0 spread of config.json, failed-unsubs.json and
unsub-log-YYYY-MM-DD.json files. Those were rewritten wholesale on every
change and could only be read back by eye; the scan cache, storage
forensics and incremental sync all need something queryable.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .paths import db_path, legacy_files

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per sender address ever seen. `trusted` is the old
-- always_trust_senders list; `blocked` records a Gmail filter we created.
CREATE TABLE IF NOT EXISTS senders (
    email                 TEXT PRIMARY KEY,
    name                  TEXT NOT NULL DEFAULT '',
    trusted               INTEGER NOT NULL DEFAULT 0,
    blocked               INTEGER NOT NULL DEFAULT 0,
    unsubscribed_at       TEXT,
    list_unsubscribe      TEXT NOT NULL DEFAULT '',
    list_unsubscribe_post TEXT NOT NULL DEFAULT '',
    first_seen            TEXT,
    last_seen             TEXT
);

-- Message metadata cache. size_estimate is what powers the Storage tab;
-- it comes free from the same fetch that reads the unsubscribe headers.
CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    sender_email  TEXT NOT NULL,
    subject       TEXT NOT NULL DEFAULT '',
    date          TEXT,
    size_estimate INTEGER NOT NULL DEFAULT 0,
    has_unsub     INTEGER NOT NULL DEFAULT 0,
    trashed       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_email);
CREATE INDEX IF NOT EXISTS idx_messages_size   ON messages(size_estimate DESC);

-- Append-only audit ledger. Replaces unsub-log-*.json.
CREATE TABLE IF NOT EXISTS actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    sender_email  TEXT NOT NULL,
    action        TEXT NOT NULL,
    method        TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '',
    ok            INTEGER NOT NULL DEFAULT 0,
    trashed_count INTEGER NOT NULL DEFAULT 0,
    dry_run       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_actions_sender ON actions(sender_email);

-- Senders whose unsubscribe attempt failed, for --retry-failed.
CREATE TABLE IF NOT EXISTS failures (
    email                 TEXT PRIMARY KEY,
    name                  TEXT NOT NULL DEFAULT '',
    failed_method         TEXT NOT NULL DEFAULT '',
    ts                    TEXT NOT NULL,
    count                 INTEGER NOT NULL DEFAULT 0,
    list_unsubscribe      TEXT NOT NULL DEFAULT '',
    list_unsubscribe_post TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    """Thin, explicit wrapper over the state database.

    Deliberately not an ORM: every query here is short and the schema is
    small enough that raw SQL stays the most readable option.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else db_path()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        try:
            self.path.chmod(0o600)  # the cache holds subject lines and addresses
        except OSError:
            pass
        self._conn.executescript(SCHEMA)
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self._conn.commit()

    # --- plumbing -------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- meta / settings ------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # --- senders --------------------------------------------------------

    def trusted_senders(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT email FROM senders WHERE trusted=1 ORDER BY email"
        ).fetchall()
        return [r["email"] for r in rows]

    def is_trusted(self, email: str) -> bool:
        row = self._conn.execute(
            "SELECT trusted FROM senders WHERE email=?", (email.lower().strip(),)
        ).fetchone()
        return bool(row and row["trusted"])

    def trust(self, email: str, name: str = "") -> None:
        """Mark a sender auto-unsubscribe. The old `a` key in the CLI."""
        email = email.lower().strip()
        if not email:
            return
        with self._tx() as c:
            c.execute(
                "INSERT INTO senders(email,name,trusted) VALUES(?,?,1) "
                "ON CONFLICT(email) DO UPDATE SET trusted=1, "
                "name=CASE WHEN senders.name='' THEN excluded.name ELSE senders.name END",
                (email, name),
            )

    def mark_blocked(self, email: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO senders(email,blocked) VALUES(?,1) "
                "ON CONFLICT(email) DO UPDATE SET blocked=1",
                (email.lower().strip(),),
            )

    def mark_unsubscribed(self, email: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO senders(email,unsubscribed_at) VALUES(?,?) "
                "ON CONFLICT(email) DO UPDATE SET unsubscribed_at=excluded.unsubscribed_at",
                (email.lower().strip(), _now()),
            )

    def upsert_sender(
        self, email: str, name: str = "", lu: str = "", lup: str = ""
    ) -> None:
        email = email.lower().strip()
        if not email:
            return
        with self._tx() as c:
            c.execute(
                """INSERT INTO senders(email,name,list_unsubscribe,list_unsubscribe_post,
                                       first_seen,last_seen)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(email) DO UPDATE SET
                     name = CASE WHEN senders.name='' THEN excluded.name ELSE senders.name END,
                     list_unsubscribe = CASE WHEN excluded.list_unsubscribe != ''
                                             THEN excluded.list_unsubscribe
                                             ELSE senders.list_unsubscribe END,
                     list_unsubscribe_post = CASE WHEN excluded.list_unsubscribe_post != ''
                                                  THEN excluded.list_unsubscribe_post
                                                  ELSE senders.list_unsubscribe_post END,
                     last_seen = excluded.last_seen""",
                (email, name, lu, lup, _now(), _now()),
            )

    # --- messages -------------------------------------------------------

    def upsert_messages(self, rows: Iterable[Sequence[Any]]) -> int:
        """Bulk-insert message metadata.

        Rows are (id, sender_email, subject, date, size_estimate, has_unsub).
        """
        rows = list(rows)
        if not rows:
            return 0
        with self._tx() as c:
            c.executemany(
                """INSERT INTO messages(id,sender_email,subject,date,size_estimate,has_unsub)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     sender_email=excluded.sender_email,
                     size_estimate=excluded.size_estimate,
                     has_unsub=excluded.has_unsub""",
                rows,
            )
        return len(rows)

    def save_scanned(self, messages) -> int:
        """Persist a batch of scanned messages and their senders.

        Called repeatedly *during* a scan rather than once at the end. A full
        scan can run for half an hour, and losing all of it because a terminal
        closed is not an acceptable failure mode.
        """
        messages = list(messages)
        if not messages:
            return 0
        self.upsert_messages(
            (m.id, m.from_email, m.subject, m.date.isoformat(),
             m.size_estimate, int(bool(m.list_unsubscribe)))
            for m in messages
        )
        for m in messages:
            self.upsert_sender(m.from_email, m.from_name,
                               m.list_unsubscribe, m.list_unsubscribe_post)
        return len(messages)

    def known_message_ids(self) -> set[str]:
        """Ids already cached, so an incremental scan can skip them."""
        return {
            r["id"]
            for r in self._conn.execute("SELECT id FROM messages WHERE trashed=0")
        }

    def mark_trashed(self, message_ids: Sequence[str]) -> None:
        if not message_ids:
            return
        with self._tx() as c:
            c.executemany(
                "UPDATE messages SET trashed=1 WHERE id=?",
                [(mid,) for mid in message_ids],
            )

    def storage_by_sender(self, limit: int = 100) -> list[sqlite3.Row]:
        """Biggest senders by total bytes — the Storage tab's headline query."""
        return self._conn.execute(
            """SELECT m.sender_email        AS email,
                      COALESCE(s.name,'')   AS name,
                      COUNT(*)              AS count,
                      SUM(m.size_estimate)  AS bytes,
                      MAX(m.has_unsub)      AS has_unsub
               FROM messages m
               LEFT JOIN senders s ON s.email = m.sender_email
               WHERE m.trashed = 0
               GROUP BY m.sender_email
               ORDER BY bytes DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    def largest_messages(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT id, sender_email, subject, date, size_estimate
               FROM messages WHERE trashed=0
               ORDER BY size_estimate DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def totals(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size_estimate),0) b "
            "FROM messages WHERE trashed=0"
        ).fetchone()
        return {"messages": row["n"], "bytes": row["b"]}

    # --- actions ledger -------------------------------------------------

    def log_action(
        self,
        sender_email: str,
        action: str,
        method: str = "",
        detail: str = "",
        ok: bool = False,
        trashed_count: int = 0,
        dry_run: bool = False,
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO actions(ts,sender_email,action,method,detail,ok,"
                "trashed_count,dry_run) VALUES(?,?,?,?,?,?,?,?)",
                (
                    _now(), sender_email, action, method, detail,
                    int(ok), trashed_count, int(dry_run),
                ),
            )

    def recent_actions(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # --- failures -------------------------------------------------------

    def record_failure(
        self, email: str, name: str, failed_method: str,
        count: int, lu: str, lup: str,
    ) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO failures(email,name,failed_method,ts,count,
                                        list_unsubscribe,list_unsubscribe_post)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(email) DO UPDATE SET
                     failed_method=excluded.failed_method, ts=excluded.ts,
                     count=excluded.count,
                     list_unsubscribe=excluded.list_unsubscribe,
                     list_unsubscribe_post=excluded.list_unsubscribe_post""",
                (email.lower().strip(), name, failed_method, _now(), count, lu, lup),
            )

    def failures(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM failures ORDER BY count DESC"
        ).fetchall()

    def clear_failure(self, email: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM failures WHERE email=?", (email.lower().strip(),))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- one-time migration from the pre-2.0 JSON files ---------------------


def import_legacy(store: Store, quiet: bool = False) -> dict[str, int]:
    """Pull the old JSON files into SQLite. Idempotent; runs once."""
    if store.get_meta("legacy_imported") == "1":
        return {}

    files = legacy_files()
    imported = {"trusted": 0, "failures": 0, "settings": 0}

    cfg_path = files["config"]
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
        for email in cfg.get("always_trust_senders", []) or []:
            store.trust(email)
            imported["trusted"] += 1
        for key in ("scan_days", "max_senders", "max_emails",
                    "sensitive_keywords", "never_trust_senders",
                    "rate_limit_seconds"):
            if key in cfg:
                store.set_setting(key, cfg[key])
                imported["settings"] += 1

    fail_path = files["failed"]
    if fail_path.exists():
        try:
            entries = json.loads(fail_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []
        for e in entries or []:
            if not e.get("email"):
                continue
            store.record_failure(
                e.get("email", ""), e.get("name", ""),
                e.get("failed_method", ""), int(e.get("count", 0) or 0),
                e.get("list_unsubscribe", ""), e.get("list_unsubscribe_post", ""),
            )
            imported["failures"] += 1

    store.set_meta("legacy_imported", "1")
    if not quiet and any(imported.values()):
        print(
            f"  • Imported previous state: {imported['trusted']} trusted senders, "
            f"{imported['failures']} pending failures."
        )
    return imported
