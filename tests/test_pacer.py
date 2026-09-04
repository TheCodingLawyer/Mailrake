"""Tests for the adaptive pacer.

Gmail meters a per-minute quota and answers 403 with no Retry-After when it
runs out. Getting this wrong is expensive in both directions: too eager and
messages get dropped, too timid and a large mailbox takes an hour. The
burst-handling test below covers a bug that made real scans 4x slower.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from gmail_unsub.gmail.scan import MAX_RATE, MIN_RATE, AdaptivePacer


def test_starts_at_the_initial_rate():
    assert AdaptivePacer(rate=18.0).rate == 18.0


def test_throttle_reduces_rate():
    p = AdaptivePacer(rate=20.0)
    p.on_throttle()
    assert p.rate < 20.0


def test_concurrent_burst_counts_as_one_event():
    """20 in-flight requests failing together describe ONE quota exhaustion.

    Reacting to each individually floored the rate on the first burst and
    made scans crawl.
    """
    p = AdaptivePacer(rate=20.0)
    for _ in range(20):
        p.on_throttle()
    assert p.throttles == 20          # all still counted for reporting
    assert p.rate == pytest.approx(14.0)  # but only one reduction applied


def test_a_later_burst_reduces_again_once_the_window_passes():
    p = AdaptivePacer(rate=20.0)
    p.on_throttle()
    first = p.rate
    p._decreased_until = time.monotonic() - 1  # simulate the window expiring
    p.on_throttle()
    assert p.rate < first


def test_rate_never_falls_below_the_floor():
    p = AdaptivePacer(rate=20.0)
    for _ in range(50):
        p._decreased_until = 0.0
        p.on_throttle()
    assert p.rate == MIN_RATE


def test_rate_never_climbs_above_the_ceiling():
    p = AdaptivePacer(rate=MAX_RATE - 1)
    for _ in range(1000):
        p.on_success()
    assert p.rate == MAX_RATE


def test_success_recovers_rate_after_a_throttle():
    p = AdaptivePacer(rate=20.0)
    p.on_throttle()
    knocked_down = p.rate
    for _ in range(25):
        p.on_success()
    assert p.rate > knocked_down


def test_retry_after_header_is_honoured_over_the_default():
    p = AdaptivePacer(rate=20.0)
    before = time.monotonic()
    p.on_throttle(retry_after=30.0)
    assert p._cooldown_until >= before + 29.0


async def test_acquire_spaces_requests_out():
    """Ten slots at 50/s should take about 0.2s, not zero."""
    p = AdaptivePacer(rate=50.0)
    start = time.monotonic()
    await asyncio.gather(*[p.acquire() for _ in range(10)])
    assert time.monotonic() - start >= 0.15


async def test_acquire_waits_out_a_cooldown():
    p = AdaptivePacer(rate=100.0)
    p.on_throttle(retry_after=0.3)
    start = time.monotonic()
    await p.acquire()
    assert time.monotonic() - start >= 0.25
