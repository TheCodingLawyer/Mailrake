"""Terminal front-end: python -m mailrake

Every flag from 1.x still works. What changed underneath is that state
lives in SQLite rather than JSON files in the working directory, and the
scan is concurrent rather than two-threaded.
"""
from __future__ import annotations

import argparse
import sys
import time

from ..core.unsubscribe import (
    describe_targets,
    parse_list_unsubscribe,
    retry_unsubscribe,
)
from ..gmail.actions import (
    block_sender,
    create_auto_trash_filter,
    find_all_from_sender,
    refetch_sender_headers,
    trash_messages,
)
from ..gmail.auth import SCOPES, SCOPES_NO_FILTERS, authenticate, build_gmail_service
from ..gmail.scan import MessageInfo, ScanError, group_by_sender, scan
from ..store.db import Store, import_legacy
from ..store.paths import config_dir
from ..store.settings import Settings
from .interactive import (
    RunSummary,
    _display_sender,
    bold,
    cyan,
    dim,
    green,
    red,
    run_interactive,
    yellow,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mailrake",
        description="Unsubscribe from Gmail senders in bulk, safely and locally. "
                    "Nothing leaves your machine.",
    )
    p.add_argument("--ui", action="store_true",
                   help="Open the browser control panel instead of the terminal UI.")
    p.add_argument("--days", type=int, default=None,
                   help="Scan window in days (default: 90). Use 0 for all time.")
    p.add_argument("--all-time", action="store_true",
                   help="Scan all mail ever. Same as --days 0.")
    p.add_argument("--all-mail", action="store_true",
                   help="Scan every category, not just Promotions/Updates/Forums. "
                        "Much slower; finds bulk senders hiding in the primary inbox.")
    p.add_argument("--max", dest="max_senders", type=int, default=None,
                   help="Max senders to process in this run (default: 200).")
    p.add_argument("--max-emails", type=int, default=None,
                   help="Max emails to fetch from Gmail (default: 2000).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would happen; do not unsubscribe or trash.")
    p.add_argument("--no-trash", action="store_true",
                   help="Unsubscribe only; leave existing mail in place.")
    p.add_argument("--no-filters", action="store_true",
                   help="Skip the gmail.settings.basic scope. Disables auto-trash "
                        "filters and blocking, for a narrower consent screen.")
    p.add_argument("--cleanup", action="store_true",
                   help="Trash all remaining mail from previously unsubscribed senders.")
    p.add_argument("--retry-failed", action="store_true",
                   help="Retry failed unsubscribes using alternative methods.")
    p.add_argument("--block-remaining", action="store_true",
                   help="Create auto-trash filters for senders that still refuse.")
    p.add_argument("--storage", action="store_true",
                   help="Show where your mailbox storage went, biggest senders first.")
    p.add_argument("--fresh", action="store_true",
                   help="Ignore the local cache and rescan from scratch.")
    p.add_argument("--where", action="store_true",
                   help="Print where this tool stores its data, and exit.")
    return p.parse_args(argv)


def _print_header() -> None:
    print(bold(cyan("\n  mailrake")))
    print(dim("  Local-first. Your mail never leaves this machine."))
    print()


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def _print_summary(summary: RunSummary, dry_run: bool) -> None:
    print()
    print(bold("  Summary"))
    print(f"    Unsubscribed:        {green(str(len(summary.unsubscribed)))}")
    print(f"    Skipped (sensitive): {yellow(str(len(summary.skipped_sensitive)))}")
    print(f"    Skipped (user):      {dim(str(len(summary.skipped_user)))}")
    print(f"    Failures:            {red(str(len(summary.failures)))}")
    if dry_run:
        print(yellow("    Mode:                DRY-RUN (no actions taken)"))
    print(dim(f"  Full history: {config_dir() / 'state.db'}"))


# --- progress reporting --------------------------------------------------


_last_phase: str | None = None


def _cli_progress(phase: str, done: int, total: int) -> None:
    """Single self-overwriting line per phase.

    Listing usually stops short of `total` (the mailbox runs out before the
    cap does), so a phase change is what ends the line, not done == total.
    """
    global _last_phase
    if _last_phase is not None and phase != _last_phase:
        print(file=sys.stderr)
    _last_phase = phase

    label = {"listing": "Listing messages",
             "fetching": "Reading metadata",
             "sweeping": "Retrying throttled messages"}.get(phase, phase)
    print(f"\r  • {label}... {done}/{total}", end="", file=sys.stderr)
    if phase in ("fetching", "sweeping") and done >= total:
        print(file=sys.stderr)
        _last_phase = None


# --- subcommands ---------------------------------------------------------


def _run_storage(store: Store) -> int:
    """Where the mailbox storage went. Uses the cached scan; no API calls."""
    totals = store.totals()
    if not totals["messages"]:
        print(yellow("  No scan cached yet. Run a scan first."))
        return 1

    print(bold(f"  {totals['messages']:,} messages scanned, "
               f"{_fmt_bytes(totals['bytes'])} total"))
    print()
    print(bold("  Biggest senders"))
    for i, row in enumerate(store.storage_by_sender(25), 1):
        tag = green(" ✓ unsub") if row["has_unsub"] else dim("   —   ")
        name = row["name"] or row["email"]
        unit = "msg " if row["count"] == 1 else "msgs"
        print(f"    {i:>2}. {_fmt_bytes(row['bytes']):>9}  {row['count']:>5} {unit} "
              f"{tag}  {cyan(name[:44])}")

    print()
    print(bold("  Largest single messages"))
    for i, row in enumerate(store.largest_messages(10), 1):
        subj = (row["subject"] or "(no subject)")[:48]
        print(f"    {i:>2}. {_fmt_bytes(row['size_estimate']):>9}  "
              f"{dim(row['sender_email'][:30]):<30} {subj}")
    return 0


def _run_cleanup(service, store: Store, dry_run: bool = False) -> int:
    """Trash everything left from senders already unsubscribed."""
    senders = store.trusted_senders()
    if not senders:
        print(green("  No unsubscribed senders on record. Run a scan first."))
        return 0

    print(dim(f"  Cleaning up mail from {len(senders)} unsubscribed senders."))
    _dry_banner(dry_run)

    total_trashed = 0
    for idx, sender in enumerate(senders, 1):
        print(f"  [{idx}/{len(senders)}] {sender}", end="")
        msg_ids = find_all_from_sender(service, sender)
        if not msg_ids:
            print(dim("  — nothing left"))
            continue
        if dry_run:
            print(yellow(f"  [PREVIEW] would trash {len(msg_ids)}"))
            continue
        count = trash_messages(service, msg_ids)
        store.mark_trashed(msg_ids)
        store.log_action(sender, "cleanup", trashed_count=count, ok=True)
        total_trashed += count
        print(green(f"  → trashed {count}"))

    print()
    print(bold("  Cleanup Summary"))
    print(f"    Senders processed: {len(senders)}")
    print(f"    Total trashed:     "
          f"{green(str(total_trashed)) if not dry_run else yellow('[preview]')}")
    return 0


def _run_retry_failed(service, store: Store, dry_run: bool = False) -> int:
    """Retry failed unsubscribes, re-fetching headers when the stored URL is stale."""
    entries = store.failures()
    if not entries:
        print(green("  No failed unsubscribes on record."))
        return 0

    print(dim(f"  Retrying {len(entries)} previously failed sender"
              f"{'s' if len(entries) != 1 else ''}."))
    _dry_banner(dry_run)

    success = still_failed = 0
    for idx, entry in enumerate(entries, 1):
        email = entry["email"]
        failed_method = entry["failed_method"]
        print(f"  [{idx}/{len(entries)}] {cyan(email)}")
        print(dim(f"      Previously failed: {failed_method}"))

        targets = parse_list_unsubscribe(entry["list_unsubscribe"])
        post = entry["list_unsubscribe_post"]
        one_click = "list-unsubscribe=one-click" in (post or "").lower()

        if not targets:
            print(dim("      Re-fetching headers from Gmail..."))
            fresh = refetch_sender_headers(service, email)
            if fresh:
                targets = parse_list_unsubscribe(fresh[0]["list_unsubscribe"])
                post = fresh[0]["list_unsubscribe_post"]
                one_click = "list-unsubscribe=one-click" in (post or "").lower()

        if not targets:
            print(red("      ✗ Still no unsubscribe target found."))
            still_failed += 1
            print()
            continue

        if dry_run:
            print(yellow(f"      [PREVIEW] would retry via "
                         f"{describe_targets(targets, one_click)}"))
            print()
            continue

        result = retry_unsubscribe(service, targets, one_click, failed_method)
        store.log_action(email, "retry", result.method, result.detail, ok=result.ok)
        if result.ok:
            print(green(f"      ✓ Retry succeeded: {result.detail}"))
            store.clear_failure(email)
            store.mark_unsubscribed(email)
            success += 1
        else:
            print(red(f"      ✗ Still failed: {result.detail}"))
            still_failed += 1
        print()

    print(bold("  Retry Summary"))
    print(f"    Succeeded:    {green(str(success))}")
    print(f"    Still failed: {red(str(still_failed))}")
    if not dry_run and still_failed:
        print(dim("  Run --block-remaining to auto-trash future mail from these."))
    return 0


def _run_block_remaining(service, store: Store, dry_run: bool = False) -> int:
    """Auto-trash filters for senders whose unsubscribe never worked."""
    entries = store.failures()
    if not entries:
        print(green("  No remaining failed senders. Nothing to block."))
        return 0

    print(dim(f"  Creating auto-trash filters for {len(entries)} sender"
              f"{'s' if len(entries) != 1 else ''}."))
    _dry_banner(dry_run)

    created = failed = 0
    for entry in entries:
        email = entry["email"]
        if dry_run:
            print(f"  [PREVIEW] would filter {cyan(email)} → auto-trash")
            created += 1
            continue
        if create_auto_trash_filter(service, email):
            store.clear_failure(email)
            store.mark_blocked(email)
            store.log_action(email, "filter", detail="auto-trash filter", ok=True)
            print(f"  ✓ Filter created: {cyan(email)} → auto-trash")
            created += 1
        else:
            print(red(f"  ✗ Filter failed: {email}"))
            failed += 1

    print()
    print(bold("  Block Summary"))
    print(f"    Filters created: {green(str(created))}")
    print(f"    Failed:          {red(str(failed))}")
    return 0


def _block_or_skip_senders(service, groups, store: Store, dry_run: bool = False) -> int:
    """Offer to block senders that provide no unsubscribe mechanism at all."""
    total = min(len(groups), 50)
    print(bold(f"  {len(groups)} senders have NO unsubscribe link."))
    print(dim("    [b]lock = mark as spam + auto-trash filter"))
    print(dim("    [n]skip  [s]skip-all  [q]quit"))
    print()

    blocked = 0
    for i in range(total):
        group = groups[i]
        if store.is_trusted(group.email):
            continue

        print(_display_sender(group, i + 1, total))
        sys.stdout.write("  [b]lock [n]skip [s]skip-all [q]quit? ")
        sys.stdout.flush()
        try:
            choice = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice in ("s", "q"):
            print(dim("  → Stopping."))
            break
        if choice == "b":
            if dry_run:
                print(yellow(f"  [PREVIEW] would block {group.email}"))
            else:
                msg_ids = [m.id for m in group.messages]
                if block_sender(service, group.email, msg_ids):
                    store.mark_blocked(group.email)
                    store.log_action(group.email, "block", ok=True,
                                     trashed_count=len(msg_ids))
                    print(green(f"  ✓ Blocked — {len(msg_ids)} to spam + filter"))
                    blocked += 1
                else:
                    print(red("  ✗ Block failed"))
        else:
            print(dim("  → Skipped."))
        print()

    return blocked


def _dry_banner(dry_run: bool) -> None:
    if not dry_run:
        return
    print()
    print(yellow("  ╔══════════════════════════════════════════════════════╗"))
    print(yellow("  ║  DRY-RUN MODE — nothing will actually happen.         ║"))
    print(yellow("  ║  This is a PREVIEW. Drop --dry-run to do real work.   ║"))
    print(yellow("  ╚══════════════════════════════════════════════════════╝"))
    print()


# --- entry point ---------------------------------------------------------


def _cached_messages(store: Store) -> list[MessageInfo]:
    """Rehydrate MessageInfo rows from the local cache."""
    from datetime import datetime

    rows = store._conn.execute(
        """SELECT m.id, m.sender_email, m.subject, m.date, m.size_estimate,
                  COALESCE(s.name,'') AS name,
                  COALESCE(s.list_unsubscribe,'') AS lu,
                  COALESCE(s.list_unsubscribe_post,'') AS lup
           FROM messages m
           LEFT JOIN senders s ON s.email = m.sender_email
           WHERE m.trashed = 0"""
    ).fetchall()

    out = []
    for r in rows:
        try:
            date = datetime.fromisoformat(r["date"]) if r["date"] else datetime.fromtimestamp(0)
        except ValueError:
            date = datetime.fromtimestamp(0)
        out.append(MessageInfo(
            id=r["id"], from_name=r["name"], from_email=r["sender_email"],
            subject=r["subject"], date=date, size_estimate=r["size_estimate"],
            list_unsubscribe=r["lu"], list_unsubscribe_post=r["lup"],
        ))
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.where:
        print(config_dir())
        return 0

    _print_header()

    store = Store()
    import_legacy(store)
    settings = Settings.load(store)

    if args.all_time:
        settings.scan_days = 0
    if args.days is not None:
        settings.scan_days = args.days
    if args.max_senders is not None:
        settings.max_senders = args.max_senders
    if args.max_emails is not None:
        settings.max_emails = args.max_emails
    if args.all_mail:
        settings.bulk_only = False

    # --storage reads the cache only; no network, no login.
    if args.storage:
        return _run_storage(store)

    if args.ui:
        from ..server.app import serve

        return serve(store, settings)

    time_label = "all time" if settings.scan_days <= 0 else f"{settings.scan_days}d"
    scope_label = "bulk categories" if settings.bulk_only else "all mail"
    print(dim(f"  Scan: {time_label} • {scope_label} • "
              f"max {settings.max_emails} emails, {settings.max_senders} senders"))
    _dry_banner(args.dry_run)

    print("  • Authenticating with Google...")
    creds = authenticate(SCOPES_NO_FILTERS if args.no_filters else SCOPES)
    service = build_gmail_service(creds)
    print(green("  ✓ Authenticated."))
    print()

    try:
        if args.cleanup:
            return _run_cleanup(service, store, dry_run=args.dry_run)
        if args.retry_failed:
            return _run_retry_failed(service, store, dry_run=args.dry_run)
        if args.block_remaining:
            return _run_block_remaining(service, store, dry_run=args.dry_run)

        started = time.time()
        skip = set() if args.fresh else store.known_message_ids()
        if skip:
            print(dim(f"  • {len(skip)} messages already cached; fetching only what's new."))

        result, history_id = scan(
            creds,
            days=settings.scan_days,
            max_emails=settings.max_emails,
            bulk_only=settings.bulk_only,
            skip_ids=skip,
            progress=_cli_progress,
        )
        infos = result.messages

        store.upsert_messages(
            (m.id, m.from_email, m.subject, m.date.isoformat(),
             m.size_estimate, int(bool(m.list_unsubscribe)))
            for m in infos
        )
        for m in infos:
            store.upsert_sender(m.from_email, m.from_name,
                                m.list_unsubscribe, m.list_unsubscribe_post)
        if history_id:
            store.set_meta("history_id", history_id)

        elapsed = time.time() - started
        if infos:
            rate = len(infos) / elapsed if elapsed else 0
            print(dim(f"  • Fetched {len(infos)} messages in {elapsed:.1f}s "
                      f"({rate:.0f}/s, settled at {result.rate:.0f} req/s)"))
        if result.failed_ids:
            # Never let these pass silently: a dropped message is a missing
            # sender and a wrong storage total.
            print(yellow(f"  ! {len(result.failed_ids)} messages could not be read "
                         f"after retries. Re-run to pick them up."))

        # An incremental run often fetches nothing new. Fall back to the cache
        # so re-running the tool shows your senders instead of an empty screen.
        if not infos and skip:
            infos = _cached_messages(store)
            print(dim(f"  • Nothing new. Showing {len(infos)} cached messages."))

        all_groups = group_by_sender(infos)
        groups = group_by_sender(infos, unsubscribable_only=True)
        print(dim(f"  • {len(all_groups)} unique senders, "
                  f"{len(groups)} with unsubscribe links"))
        print()

        summary = RunSummary()
        if groups:
            summary = run_interactive(
                service, groups, settings,
                dry_run=args.dry_run, trash_enabled=not args.no_trash,
            )

        unsub_emails = {g.email for g in groups}
        non_unsub = [g for g in all_groups if g.email not in unsub_emails]
        blocked = 0
        if non_unsub and not args.no_filters:
            print()
            blocked = _block_or_skip_senders(service, non_unsub, store,
                                             dry_run=args.dry_run)

        _print_summary(summary, dry_run=args.dry_run)
        if non_unsub:
            print()
            print(dim(f"  Also saw {len(non_unsub)} senders with no unsubscribe header."))
            print(f"    Blocked: {green(str(blocked))}   "
                  f"Skipped: {dim(str(len(non_unsub) - blocked))}")
        return 0

    except KeyboardInterrupt:
        print()
        print(yellow("  • Cancelled by user."))
        return 1
    except ScanError as e:
        print()
        print(red(f"  ✗ {e}"))
        return 1
    finally:
        store.close()


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    run()
