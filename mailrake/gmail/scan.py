"""Mailbox scanning.

Replaces the pre-2.0 `fetch_message_infos`, which issued one hand-rolled
`requests.get` per message across two worker threads and dug the bearer
token out of `service._http.credentials` (a private attribute).

Three changes carry the speedup:

1. Async fetch at real concurrency instead of two threads.
2. A narrowed default query, so bulk mail is found without walking the
   whole mailbox.
3. `sizeEstimate` in the field mask, so one pass feeds both the
   unsubscribe view and the storage view.

On concurrency: Gmail allows 250 quota units per user per second and
`messages.get` costs 5, so roughly 50 messages/second is the hard ceiling
no matter how wide we open the pipe. Batching 100 sub-requests into one
HTTP call would hit that same wall, so the simpler async client wins —
it is easier to make resumable, cancellable and correct under backoff.
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import Callable, Iterable, Sequence

import httpx

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

METADATA_HEADERS = [
    "From",
    "Subject",
    "Date",
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
]

# Where bulk mail actually lives. Newsletters and promotional senders land
# in these categories; scanning them instead of the whole mailbox cuts the
# candidate set hard before a single metadata fetch happens.
BULK_QUERY = "(category:promotions OR category:updates OR category:forums) -in:chats"

# Gmail meters a per-user *per-minute* budget ("Total Query Cost", units per
# minute per user) and returns 403 -- not 429, and with no Retry-After -- when
# you exhaust it. Measured on a real mailbox: a flat 15 concurrent requests
# sustains ~36 req/s and loses ~18% of messages to 403s. Guessing a safe fixed
# rate is fragile because the budget varies per project, so the pacer below
# finds the ceiling by observation instead.
# Measured against a real mailbox at fixed rates, 25s per probe:
#
#     rate   403 loss
#      5/s      0.0%
#     10/s      0.0%
#     15/s      9.3%
#     25/s     20.1%
#
# The knee sits between 10 and 15. Starting above it (an earlier build began
# at 18) just throttles immediately and thrashes down to the floor, so start
# at the known-good rate and let the pacer probe a little above it.
#
# These numbers are one project's quota, not a universal constant, which is
# the reason the pacer adapts instead of hardcoding a rate.
INITIAL_RATE = 10.0     # requests/second at start
MIN_RATE = 3.0
MAX_RATE = 14.0
MAX_ATTEMPTS = 8
# Gmail meters per *minute*, so a short pause just walks back into the wall.
COOLDOWN_SECONDS = 6.0
# Pause before the final sweep, long enough for the per-minute budget to refill.
SWEEP_PAUSE_SECONDS = 45.0
MAX_CONCURRENCY = 12    # ceiling on in-flight requests; the pacer sets the pace

ProgressFn = Callable[[str, int, int], None]
BatchFn = Callable[[list["MessageInfo"]], None]


class AdaptivePacer:
    """Finds Gmail's sustainable request rate by watching for throttling.

    Additive increase, multiplicative decrease. A throttle halves the rate
    and parks every worker on a shared cooldown, so the whole pool eases off
    together instead of each request retrying into the same wall.
    """

    def __init__(
        self,
        rate: float = INITIAL_RATE,
        min_rate: float = MIN_RATE,
        max_rate: float = MAX_RATE,
    ) -> None:
        self.rate = rate
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.throttles = 0
        self._successes = 0
        self._next_slot = 0.0
        self._cooldown_until = 0.0
        self._decreased_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait for this request's turn."""
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot, self._cooldown_until)
            self._next_slot = slot + 1.0 / self.rate
        delay = slot - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def on_throttle(self, retry_after: float | None = None) -> None:
        """Register a throttle, but cut the rate at most once per window.

        Requests run concurrently, so exhausting the quota produces a burst
        of 403s that all describe the *same* event. Halving once per response
        would floor the rate on the first burst; halving once per window
        tracks the actual signal.
        """
        self.throttles += 1
        now = time.monotonic()
        if now < self._decreased_until:
            return  # already reacted to this burst

        self._successes = 0
        self.rate = max(self.min_rate, self.rate * 0.7)
        pause = retry_after if retry_after else COOLDOWN_SECONDS
        self._cooldown_until = max(self._cooldown_until, now + pause)
        # Ignore further 403s until the cooldown has actually been served.
        self._decreased_until = self._cooldown_until

    def on_success(self) -> None:
        self._successes += 1
        if self._successes >= 25:
            self._successes = 0
            self.rate = min(self.max_rate, self.rate + 1.5)


@dataclass
class FetchResult:
    """Messages fetched, plus the ids we genuinely could not get.

    Failures are returned rather than swallowed: a dropped message is a
    missing sender and a wrong storage total, so the caller must be able
    to see and re-queue them.
    """

    messages: list["MessageInfo"] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    throttles: int = 0
    rate: float = 0.0


@dataclass
class MessageInfo:
    id: str
    from_name: str
    from_email: str
    subject: str
    date: datetime
    size_estimate: int = 0
    list_unsubscribe: str = ""
    list_unsubscribe_post: str = ""

    @property
    def display_sender(self) -> str:
        return f"{self.from_name} <{self.from_email}>" if self.from_name else self.from_email


@dataclass
class SenderGroup:
    name: str
    email: str
    messages: list[MessageInfo] = field(default_factory=list)

    @property
    def display_sender(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email

    @property
    def count(self) -> int:
        return len(self.messages)

    @property
    def total_bytes(self) -> int:
        return sum(m.size_estimate for m in self.messages)

    @property
    def last_date(self) -> datetime:
        return max(m.date for m in self.messages)

    @property
    def sample_subjects(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for m in self.messages:
            subj = (m.subject or "").strip()
            if subj and subj not in seen:
                seen.add(subj)
                out.append(subj)
            if len(out) >= 2:
                break
        return out

    @property
    def list_unsubscribe(self) -> str:
        return next((m.list_unsubscribe for m in self.messages if m.list_unsubscribe), "")

    @property
    def list_unsubscribe_post(self) -> str:
        return next(
            (m.list_unsubscribe_post for m in self.messages if m.list_unsubscribe_post), ""
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "email": self.email,
            "count": self.count,
            "bytes": self.total_bytes,
            "last_date": self.last_date.isoformat(),
            "sample_subjects": self.sample_subjects,
            "list_unsubscribe": self.list_unsubscribe,
            "list_unsubscribe_post": self.list_unsubscribe_post,
        }


# --- header parsing (behaviour preserved from 1.x) ----------------------


def _parse_from_header(from_header: str) -> tuple[str, str]:
    name, email = parseaddr(from_header or "")
    if not email:
        m = re.search(r"<([^>]+)>", from_header or "")
        email = m.group(1).strip() if m else (from_header or "").strip()
    return name.strip(), email.lower()


def _parse_date(date_header: str) -> datetime:
    if not date_header:
        return datetime.fromtimestamp(0)
    try:
        dt = parsedate_to_datetime(date_header)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except (TypeError, ValueError):
        return datetime.fromtimestamp(0)


def _headers_to_dict(headers: list[dict]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (headers or [])}


def _message_from_payload(msg: dict) -> MessageInfo | None:
    hdrs = _headers_to_dict(msg.get("payload", {}).get("headers", []))
    name, email = _parse_from_header(hdrs.get("from", ""))
    if not email:
        return None
    return MessageInfo(
        id=msg["id"],
        from_name=name,
        from_email=email,
        subject=hdrs.get("subject", ""),
        date=_parse_date(hdrs.get("date", "")),
        size_estimate=int(msg.get("sizeEstimate", 0) or 0),
        list_unsubscribe=hdrs.get("list-unsubscribe", ""),
        list_unsubscribe_post=hdrs.get("list-unsubscribe-post", ""),
    )


# --- credentials ---------------------------------------------------------


def _bearer(creds) -> str:
    """Current access token, refreshed if needed, via the public API."""
    if not creds.valid:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
    return creds.token


def build_query(days: int, bulk_only: bool = True, extra: str = "") -> str:
    """Compose the Gmail search query for a scan."""
    parts: list[str] = []
    if days and days > 0:
        parts.append(f"newer_than:{days}d")
    if bulk_only:
        parts.append(BULK_QUERY)
    if extra:
        parts.append(extra)
    return " ".join(parts)


# --- listing -------------------------------------------------------------


class ScanError(RuntimeError):
    """Scanning could not proceed -- shown to the user, never as a traceback."""


async def list_message_ids(
    client: httpx.AsyncClient,
    creds,
    query: str,
    max_results: int,
    progress: ProgressFn | None = None,
    pacer: AdaptivePacer | None = None,
) -> list[str]:
    """Page through messages.list.

    Listing is only one quota unit per call, but it draws on the same
    per-minute budget as everything else, so it can be throttled too --
    and being throttled here used to abort the whole scan with a traceback.
    """
    pacer = pacer or AdaptivePacer()
    ids: list[str] = []
    page_token: str | None = None

    while len(ids) < max_results:
        params = {
            "q": query,
            "maxResults": str(min(500, max_results - len(ids))),
            "fields": "messages(id),nextPageToken",
        }
        if page_token:
            params["pageToken"] = page_token

        data = None
        for attempt in range(MAX_ATTEMPTS):
            await pacer.acquire()
            try:
                resp = await client.get(
                    f"{GMAIL_API}/messages",
                    params=params,
                    headers={"Authorization": f"Bearer {_bearer(creds)}"},
                )
            except httpx.HTTPError as e:
                if attempt == MAX_ATTEMPTS - 1:
                    raise ScanError(f"Could not reach Gmail: {e}") from e
                await asyncio.sleep(1.0 * (attempt + 1))
                continue

            if resp.status_code in (403, 429, 500, 502, 503):
                retry_after = resp.headers.get("Retry-After")
                pacer.on_throttle(float(retry_after) if retry_after else None)
                continue
            if resp.status_code == 401:
                raise ScanError("Gmail rejected the login. Re-run to sign in again.")
            if resp.status_code != 200:
                raise ScanError(
                    f"Gmail returned HTTP {resp.status_code} while listing messages."
                )

            data = resp.json()
            pacer.on_success()
            break

        if data is None:
            raise ScanError(
                "Gmail kept refusing the request (quota exhausted). "
                "Wait a minute and run again -- progress so far is cached."
            )

        ids.extend(m["id"] for m in data.get("messages", []))
        if progress:
            progress("listing", len(ids), max_results)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return ids[:max_results]


# --- metadata fetch ------------------------------------------------------


async def _fetch_one(
    client: httpx.AsyncClient,
    creds,
    msg_id: str,
    pacer: AdaptivePacer,
    sem: asyncio.Semaphore,
) -> MessageInfo | None:
    """Fetch one message's metadata, yielding to the pacer on every attempt."""
    params = [
        ("format", "metadata"),
        ("fields", "id,sizeEstimate,payload/headers"),
    ] + [("metadataHeaders", h) for h in METADATA_HEADERS]

    for attempt in range(MAX_ATTEMPTS):
        await pacer.acquire()
        async with sem:
            try:
                resp = await client.get(
                    f"{GMAIL_API}/messages/{msg_id}",
                    params=params,
                    headers={"Authorization": f"Bearer {_bearer(creds)}"},
                )
            except httpx.HTTPError:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

        # Gmail signals quota exhaustion with 403, not 429, and sends no
        # Retry-After. Treat both alike and let the pacer decide the pause.
        if resp.status_code in (403, 429, 500, 502, 503):
            retry_after = resp.headers.get("Retry-After")
            pacer.on_throttle(float(retry_after) if retry_after else None)
            continue

        if resp.status_code != 200:
            return None  # 404 or similar: the message is genuinely gone

        pacer.on_success()
        try:
            return _message_from_payload(resp.json())
        except (KeyError, ValueError):
            return None

    return None  # attempts exhausted -- caller records this as a failure


async def fetch_metadata(
    creds,
    ids: Sequence[str],
    progress: ProgressFn | None = None,
    pacer: AdaptivePacer | None = None,
    sweep: bool = True,
    on_batch: BatchFn | None = None,
    batch_size: int = 50,
) -> FetchResult:
    """Fetch metadata for every id, as fast as Gmail will actually allow.

    `on_batch` is handed messages as they arrive rather than at the end. A
    full scan can run for half an hour; without this, closing the terminal
    threw the whole thing away.
    """
    result = FetchResult()
    if not ids:
        return result

    pacer = pacer or AdaptivePacer()
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    done = 0
    total = len(ids)

    limits = httpx.Limits(
        max_connections=MAX_CONCURRENCY,
        max_keepalive_connections=MAX_CONCURRENCY,
    )
    pending: list[MessageInfo] = []

    def flush() -> None:
        if on_batch and pending:
            on_batch(list(pending))
        pending.clear()

    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        tasks = {
            asyncio.create_task(_fetch_one(client, creds, mid, pacer, sem)): mid
            for mid in ids
        }
        try:
            for task in asyncio.as_completed(tasks):
                info = await task
                if info is not None:
                    result.messages.append(info)
                    pending.append(info)
                    if len(pending) >= batch_size:
                        flush()
                done += 1
                if progress and (done % 25 == 0 or done == total):
                    progress("fetching", done, total)
        finally:
            # Runs on Ctrl+C and on cancellation too, so an interrupted scan
            # keeps everything it had already fetched.
            flush()
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Collect the cancelled siblings, otherwise asyncio prints
            # "Task exception was never retrieved" tracebacks over the top of
            # our own cancellation message.
            await asyncio.gather(*tasks, return_exceptions=True)

    fetched = {m.id for m in result.messages}
    leftover = [mid for mid in ids if mid not in fetched]

    # Under sustained quota pressure a message can burn all its attempts and
    # still not land. Rather than push that onto the user as "re-run me", take
    # one more pass at the stragglers after the budget has had time to refill.
    if leftover and sweep:
        if progress:
            progress("sweeping", 0, len(leftover))
        await asyncio.sleep(SWEEP_PAUSE_SECONDS)
        sweep_pacer = AdaptivePacer(rate=MIN_RATE)
        async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
            swept = await asyncio.gather(*[
                _fetch_one(client, creds, mid, sweep_pacer, sem) for mid in leftover
            ])
        recovered = [m for m in swept if m is not None]
        result.messages.extend(recovered)
        if on_batch and recovered:
            on_batch(recovered)
        if progress:
            progress("sweeping", len(leftover), len(leftover))
        fetched |= {m.id for m in recovered}
        leftover = [mid for mid in leftover if mid not in fetched]

    result.failed_ids = leftover
    result.throttles = pacer.throttles
    result.rate = pacer.rate
    return result


async def scan_async(
    creds,
    days: int,
    max_emails: int,
    bulk_only: bool = True,
    skip_ids: set[str] | None = None,
    progress: ProgressFn | None = None,
    on_batch: BatchFn | None = None,
) -> tuple[FetchResult, str | None]:
    """Full scan. Returns the fetch result and the mailbox's current historyId."""
    query = build_query(days, bulk_only)

    # One pacer for the whole scan: both phases draw on the same quota, so
    # what listing learns about the limit should carry into fetching.
    pacer = AdaptivePacer()

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {_bearer(creds)}"}
        profile = await client.get(f"{GMAIL_API}/profile", headers=headers)
        history_id = profile.json().get("historyId") if profile.status_code == 200 else None

        ids = await list_message_ids(client, creds, query, max_emails, progress, pacer)

    if skip_ids:
        ids = [i for i in ids if i not in skip_ids]

    result = await fetch_metadata(creds, ids, progress, pacer, on_batch=on_batch)
    return result, history_id


def scan(
    creds,
    days: int,
    max_emails: int,
    bulk_only: bool = True,
    skip_ids: set[str] | None = None,
    progress: ProgressFn | None = None,
    on_batch: BatchFn | None = None,
) -> tuple[FetchResult, str | None]:
    """Blocking wrapper for CLI callers."""
    return asyncio.run(
        scan_async(creds, days, max_emails, bulk_only, skip_ids, progress, on_batch)
    )


# --- grouping ------------------------------------------------------------


def group_by_sender(
    infos: Iterable[MessageInfo], unsubscribable_only: bool = False
) -> list[SenderGroup]:
    """Group messages by sender, heaviest first.

    `unsubscribable_only` reproduces 1.x's `group_by_sender`; the default
    reproduces `group_all_by_sender`. One function, one flag.
    """
    groups: dict[str, SenderGroup] = {}
    for info in infos:
        if unsubscribable_only and not info.list_unsubscribe:
            continue
        group = groups.get(info.from_email)
        if group is None:
            group = groups[info.from_email] = SenderGroup(
                name=info.from_name, email=info.from_email
            )
        group.messages.append(info)
        if not group.name and info.from_name:
            group.name = info.from_name

    out = list(groups.values())
    out.sort(key=lambda g: g.count, reverse=True)
    return out
