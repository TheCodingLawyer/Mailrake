"""An interrupted scan must keep what it already fetched.

A full mailbox scan can run for half an hour. The first build persisted
nothing until the very end, so closing the terminal threw all of it away --
which is exactly what happened in practice. These tests are the guardrail.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from mailrake.gmail import scan as mod
from mailrake.store.db import Store


def _msg(i: int):
    return mod.MessageInfo(
        id=f"m{i}", from_name="Sender", from_email=f"s{i % 3}@ex.com",
        subject=f"subject {i}", date=datetime(2026, 1, 1),
        size_estimate=1000 + i, list_unsubscribe="<https://ex.com/u>",
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


class TestIncrementalPersistence:
    async def test_results_are_handed_over_in_batches_not_one_lump(self, monkeypatch):
        """Multiple on_batch calls prove work is flushed during the scan.

        A single call at the end is the old behaviour, and it is what made an
        interrupted scan worthless.
        """
        batches: list[int] = []

        async def ok(client, creds, msg_id, pacer, sem):
            return _msg(int(msg_id[1:]))

        monkeypatch.setattr(mod, "_fetch_one", ok)

        await mod.fetch_metadata(
            None, [f"m{i}" for i in range(120)],
            on_batch=lambda b: batches.append(len(b)), batch_size=50,
        )
        assert len(batches) == 3          # 50, 50, then a final flush of 20
        assert batches[:2] == [50, 50]
        assert sum(batches) == 120

    async def test_store_persists_a_partial_scan(self, store, monkeypatch):
        async def ok(client, creds, msg_id, pacer, sem):
            return _msg(int(msg_id[1:]))

        monkeypatch.setattr(mod, "_fetch_one", ok)
        await mod.fetch_metadata(
            None, [f"m{i}" for i in range(60)],
            on_batch=store.save_scanned, batch_size=25,
        )
        assert store.totals()["messages"] == 60
        assert store.known_message_ids() >= {"m0", "m59"}

    async def test_interrupted_scan_keeps_what_it_had(self, store, monkeypatch):
        """The failure that lost a real 1,000-message scan."""
        monkeypatch.setattr(mod, "SWEEP_PAUSE_SECONDS", 0.0)
        count = 0

        class Interrupted(Exception):
            """Stands in for Ctrl+C / the terminal closing.

            pytest aborts the whole session on a real KeyboardInterrupt; this
            travels the identical path through fetch_metadata's finally block.
            """

        async def fails_partway(client, creds, msg_id, pacer, sem):
            nonlocal count
            count += 1
            if count > 70:
                raise Interrupted("user closed the terminal")
            return _msg(int(msg_id[1:]))

        monkeypatch.setattr(mod, "_fetch_one", fails_partway)

        with pytest.raises(Interrupted):
            await mod.fetch_metadata(
                None, [f"m{i}" for i in range(200)],
                on_batch=store.save_scanned, batch_size=25,
            )

        # The work done before the interrupt survived.
        saved = store.totals()["messages"]
        assert saved > 0, "an interrupted scan saved nothing -- the original bug"
        assert saved <= 70

    async def test_a_resumed_scan_skips_what_is_cached(self, store, monkeypatch):
        async def ok(client, creds, msg_id, pacer, sem):
            return _msg(int(msg_id[1:]))

        monkeypatch.setattr(mod, "_fetch_one", ok)
        await mod.fetch_metadata(None, ["m1", "m2"], on_batch=store.save_scanned)

        known = store.known_message_ids()
        remaining = [i for i in ["m1", "m2", "m3"] if i not in known]
        assert remaining == ["m3"]
