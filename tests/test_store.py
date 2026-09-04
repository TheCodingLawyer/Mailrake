"""Tests for local state: the parts that must survive a restart."""
from __future__ import annotations

import pytest

from gmail_unsub.store.db import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_trust_is_idempotent(store):
    store.trust("a@ex.com", "A")
    store.trust("a@ex.com", "A")
    assert store.trusted_senders() == ["a@ex.com"]
    assert store.is_trusted("A@EX.COM")  # case-insensitive


def test_failures_upsert_rather_than_duplicate(store):
    store.record_failure("x@ex.com", "X", "https", 3, "<https://a>", "")
    store.record_failure("x@ex.com", "X", "mailto", 5, "<https://a>", "")
    rows = store.failures()
    assert len(rows) == 1
    assert rows[0]["failed_method"] == "mailto"
    assert rows[0]["count"] == 5

    store.clear_failure("x@ex.com")
    assert store.failures() == []


def test_storage_rollup_sorts_by_bytes_not_count(store):
    # One huge message must outrank many small ones — that ordering is the
    # whole point of the storage view.
    store.upsert_messages([
        ("m1", "big@ex.com", "s", "2026-01-01", 9_000_000, 1),
        ("m2", "small@ex.com", "s", "2026-01-01", 1_000, 0),
        ("m3", "small@ex.com", "s", "2026-01-01", 1_000, 0),
        ("m4", "small@ex.com", "s", "2026-01-01", 1_000, 0),
    ])
    rows = store.storage_by_sender()
    assert rows[0]["email"] == "big@ex.com"
    assert rows[0]["bytes"] == 9_000_000
    assert store.totals() == {"messages": 4, "bytes": 9_003_000}


def test_trashed_messages_leave_the_storage_totals(store):
    store.upsert_messages([("m1", "a@ex.com", "s", "2026-01-01", 5_000, 1)])
    store.mark_trashed(["m1"])
    assert store.totals()["messages"] == 0
    assert store.storage_by_sender() == []


def test_known_ids_drive_incremental_scan(store):
    store.upsert_messages([("m1", "a@ex.com", "s", "2026-01-01", 10, 1)])
    assert store.known_message_ids() == {"m1"}


def test_action_ledger_is_append_only(store):
    store.log_action("a@ex.com", "unsubscribe", "https", "200", ok=True, trashed_count=4)
    store.log_action("a@ex.com", "unsubscribe", "https", "500", ok=False)
    rows = store.recent_actions()
    assert len(rows) == 2
    assert rows[0]["ok"] == 0  # newest first
    assert rows[1]["trashed_count"] == 4
