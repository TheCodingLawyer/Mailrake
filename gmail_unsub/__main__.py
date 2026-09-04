"""CLI entry point: python -m gmail_unsub"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .auth import authenticate, build_gmail_service
from .config import Config
from .gmail_client import fetch_message_infos, group_all_by_sender, group_by_sender
from .interactive import (
    RunSummary,
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
        prog="gmail-unsub",
        description="Safely unsubscribe from Gmail senders with explicit confirmation. "
                    "Use --cleanup after unsubscribing to trash all remaining emails "
                    "from those senders.",
    )
    p.add_argument(
        "--days", type=int, default=None,
        help="Scan window in days (default: 90). Use 0 for all time.",
    )
    p.add_argument(
        "--all-time", action="store_true",
        help="Scan ALL emails ever. Same as --days 0.",
    )
    p.add_argument(
        "--max", dest="max_senders", type=int, default=None,
        help="Max senders to process in this run (default: 200).",
    )
    p.add_argument(
        "--max-emails", type=int, default=None,
        help="Max emails to fetch from Gmail (default: 2000).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen; do not unsubscribe or trash.",
    )
    p.add_argument(
        "--no-trash", action="store_true",
        help="Unsubscribe only; do not move existing emails to trash.",
    )
    p.add_argument(
        "--cleanup", action="store_true",
        help="Trash all remaining emails from previously unsubscribed senders.",
    )
    p.add_argument(
        "--retry-failed", action="store_true",
        help="Retry unsubscribing from all previously failed senders using alternative methods.",
    )
    p.add_argument(
        "--block-remaining", action="store_true",
        help="Create Gmail auto-trash filters for senders that still can't be unsubscribed.",
    )
    p.add_argument(
        "--config", type=str, default=None,
        help="Path to a custom config.json.",
    )
    return p.parse_args(argv)


def _print_header() -> None:
    print(bold(cyan("\n  Gmail Auto-Unsubscribe")))
    print(dim("  Properly unsubscribe using RFC 8058 one-click or mailto."))
    print()


def _print_summary(summary: RunSummary, dry_run: bool) -> None:
    print()
    print(bold("  Summary"))
    print(f"    Unsubscribed:  {green(str(len(summary.unsubscribed)))}")
    print(f"    Skipped (sensitive): {yellow(str(len(summary.skipped_sensitive)))}")
    print(f"    Skipped (user):  {dim(str(len(summary.skipped_user)))}")
    print(f"    Failures:       {red(str(len(summary.failures)))}")
    if dry_run:
        print(yellow("    Mode:           DRY-RUN (no actions taken)"))


def _write_log(summary: RunSummary, dry_run: bool) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    suffix = "-dryrun" if dry_run else ""
    path = Path(f"unsub-log-{today}{suffix}.json")
    payload = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "unsubscribed": [r.__dict__ for r in summary.unsubscribed],
        "skipped_sensitive": summary.skipped_sensitive,
        "skipped_user": summary.skipped_user,
        "failures": [r.__dict__ for r in summary.failures],
    }
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        print(yellow(f"  ! Could not write log: {e}"))
        return path
    print(dim(f"  Log saved: {path}"))
    return path


def _run_cleanup(service, config, dry_run: bool = False) -> int:
    """Trash all emails from every sender in the always_trust_senders list."""
    from .gmail_client import find_all_from_sender, trash_messages

    senders = config.always_trust_senders
    if not senders:
        print(green("  No unsubscribed senders in config. Run the main tool first."))
        return 0

    print(dim(f"  Cleaning up emails from {len(senders)} previously unsubscribed senders."))
    if dry_run:
        print()
        print(yellow("  ╔══════════════════════════════════════════════════════╗"))
        print(yellow("  ║  DRY-RUN MODE — nothing will actually happen.        ║"))
        print(yellow("  ╚══════════════════════════════════════════════════════╝"))
    print()

    total_trashed = 0
    for idx, sender in enumerate(senders, 1):
        print(f"  [{idx}/{len(senders)}] Searching for {sender}...")
        msg_ids = find_all_from_sender(service, sender)
        if not msg_ids:
            print(dim(f"      No messages found."))
            continue
        print(f"      Found {len(msg_ids)} emails", end="")
        if dry_run:
            print(yellow("  [PREVIEW — would trash]"))
        else:
            count = trash_messages(service, msg_ids)
            total_trashed += count
            print(green(f"  → trashed {count}"))
        print()

    print(bold("  Cleanup Summary"))
    print(f"    Senders processed: {len(senders)}")
    print(f"    Total trashed:     {green(str(total_trashed)) if not dry_run else yellow('[preview]')}")
    return 0


def _run_block_remaining(service, dry_run: bool = False) -> int:
    """Create Gmail auto-trash filters for remaining entries in failed-unsubs.json."""
    from .gmail_client import create_auto_trash_filter
    from .interactive import load_failed_log, _remove_from_failed_log

    entries = load_failed_log()
    if not entries:
        print(green("  No remaining failed senders. Nothing to block."))
        return 0

    print(dim(f"  Creating auto-trash filters for {len(entries)} sender{'s' if len(entries) != 1 else ''}."))
    if dry_run:
        print()
        print(yellow("  ╔══════════════════════════════════════════════════════╗"))
        print(yellow("  ║  DRY-RUN MODE — nothing will actually happen.        ║"))
        print(yellow("  ╚══════════════════════════════════════════════════════╝"))
    print()

    created = 0
    failed = 0
    for entry in entries:
        email = entry.get("email", "")
        if dry_run:
            print(f"  [PREVIEW] Would create filter: {cyan(email)} → auto-trash")
            created += 1
            continue

        ok = create_auto_trash_filter(service, email)
        if ok:
            _remove_from_failed_log(email)
            print(f"  ✓ Filter created: {cyan(email)} → auto-trash")
            created += 1
        else:
            print(red(f"  ✗ Filter failed: {email}"))
            failed += 1

    print()
    print(bold("  Block Summary"))
    print(f"    Filters created: {green(str(created))}")
    print(f"    Failed:          {red(str(failed))}")
    if not dry_run:
        print(dim("  These filters permanently auto-trash future emails from these senders."))
    return 0


def _run_retry_failed(service, config, dry_run: bool = False) -> int:
    """Retry unsubscribing from all senders in the persistent failure log using alternative methods."""
    from .gmail_client import refetch_sender_headers, trash_messages
    from .interactive import load_failed_log, _remove_from_failed_log
    from .unsubscribe import parse_list_unsubscribe, retry_unsubscribe

    entries = load_failed_log()
    if not entries:
        print(green("  No failed unsubscribes on record."))
        return 0

    print(dim(f"  Retrying {len(entries)} previously failed sender{'s' if len(entries) != 1 else ''}."))
    if dry_run:
        print()
        print(yellow("  ╔══════════════════════════════════════════════════════╗"))
        print(yellow("  ║  DRY-RUN MODE — nothing will actually happen.        ║"))
        print(yellow("  ╚══════════════════════════════════════════════════════╝"))
    print()

    success = 0
    still_failed = 0
    for idx, entry in enumerate(entries, 1):
        email = entry.get("email", "")
        name = entry.get("name", "")
        failed_method = entry.get("failed_method", "")
        display = f"{name} <{email}>" if name else email

        print(f"  [{idx}/{len(entries)}] {cyan(email)}")
        print(dim(f"      Previously failed: {failed_method} "))

        targets = parse_list_unsubscribe(entry.get("list_unsubscribe", ""))
        post = entry.get("list_unsubscribe_post", "")
        one_click = "list-unsubscribe=one-click" in (post or "").lower()

        if not targets:
            print(dim("      Re-fetching headers from Gmail..."))
            fresh = refetch_sender_headers(service, email)
            if fresh:
                targets = parse_list_unsubscribe(fresh[0].get("list_unsubscribe", ""))
                post = fresh[0].get("list_unsubscribe_post", "")
                one_click = "list-unsubscribe=one-click" in (post or "").lower()

        if not targets:
            print(red("      ✗ Still no unsubscribe target found."))
            still_failed += 1
            print()
            continue

        if dry_run:
            from .unsubscribe import describe_targets
            print(yellow(f"      [PREVIEW] Would retry with: {describe_targets(targets, one_click)}"))
            print()
            continue

        result = retry_unsubscribe(service, targets, one_click, failed_method)
        if result.ok:
            print(green(f"      ✓ Retry succeeded: {result.detail}"))
            _remove_from_failed_log(email)
            success += 1
        else:
            print(red(f"      ✗ Still failed: {result.detail}"))
            still_failed += 1
        print()

    print(bold("  Retry Summary"))
    print(f"    Succeeded:       {green(str(success))}")
    print(f"    Still failed:    {red(str(still_failed))}")
    if not dry_run and still_failed == 0:
        print(green("  All failures resolved!"))
    elif still_failed > 0:
        print(dim("  Run --block-remaining to auto-trash future emails from remaining senders."))
    return 0


def _block_or_skip_senders(service, groups, config, dry_run: bool = False) -> int:
    """Let the user block non-unsubscribeable senders via Gmail spam + filters."""
    from .gmail_client import create_auto_trash_filter, trash_messages
    from .interactive import _display_sender

    total = min(len(groups), 50)
    print(bold(f"  {len(groups)} senders have NO unsubscribe link."))
    print(dim("    [b]lock = mark as spam + auto-spam filter (Gmail's 'block' equivalent)"))
    print(dim("    [n]skip  [s]skip-all  [q]quit"))
    print()

    blocked = 0
    for i in range(total):
        group = groups[i]
        if group.email.lower() in (s.lower() for s in config.always_trust_senders):
            continue

        print(_display_sender(group, i + 1, total))
        sys.stdout.write(f"  [b]lock [n]skip [s]skip-all [q]quit? ")
        sys.stdout.flush()
        try:
            choice = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if choice == "s":
            print(dim("  → Skipping all."))
            break
        if choice == "q":
            print(dim("  → Quit."))
            break
        if choice == "n":
            print(dim("  → Skipped."))
            print()
            continue
        if choice == "b":
            if dry_run:
                print(yellow(f"  [PREVIEW] Would block {group.email} (spam + filter)"))
            else:
                msg_ids = [m.id for m in group.messages]
                if _spam_and_filter(service, group.email, msg_ids):
                    print(green(f"  ✓ Blocked — {len(msg_ids)} marked spam + auto-spam filter"))
                    blocked += 1
                else:
                    print(red("  ✗ Block failed"))
            print()
            continue
        print(f"  {dim('(invalid; choose b/n/s/q)')}")

    return blocked


def _spam_and_filter(service, email: str, msg_ids: list) -> bool:
    """Mark messages as spam and create a filter to auto-spam future emails."""
    from googleapiclient.errors import HttpError

    if msg_ids:
        for i in range(0, len(msg_ids), 1000):
            try:
                service.users().messages().batchModify(
                    userId="me",
                    body={"ids": msg_ids[i:i+1000], "addLabelIds": ["SPAM"]},
                ).execute()
            except HttpError as e:
                print(f"  ! Spam mark error: {e}", file=sys.stderr)
                return False

    try:
        service.users().settings().filters().create(
            userId="me",
            body={
                "criteria": {"from": email},
                "action": {"removeLabelIds": ["INBOX"], "addLabelIds": ["TRASH"]},
            },
        ).execute()
        return True
    except HttpError as e:
        detail = str(e)
        if "already exists" in detail.lower() or "duplicate" in detail.lower():
            return True
        print(f"  ! Filter error: {e}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _print_header()

    config = Config.load(args.config)
    if args.all_time:
        config.scan_days = 0
    if args.days is not None:
        config.scan_days = args.days
    if args.max_senders is not None:
        config.max_senders = args.max_senders
    if args.max_emails is not None:
        config.max_emails = args.max_emails

    time_label = "all time" if config.scan_days <= 0 else f"{config.scan_days}d"
    print(dim(f"  Config: scan={time_label}, max_senders={config.max_senders}, "
              f"max_emails={config.max_emails}"))
    if args.dry_run:
        print()
        print(yellow("  ╔══════════════════════════════════════════════════════╗"))
        print(yellow("  ║  DRY-RUN MODE — nothing will actually happen.        ║"))
        print(yellow("  ║  This is a PREVIEW. Drop --dry-run to do real work.  ║"))
        print(yellow("  ╚══════════════════════════════════════════════════════╝"))
        print()

    print("  • Authenticating with Google...")
    creds = authenticate()
    service = build_gmail_service(creds)
    print(green("  ✓ Authenticated."))
    print()

    try:
        if args.cleanup:
            return _run_cleanup(service, config, dry_run=args.dry_run)

        if args.retry_failed:
            return _run_retry_failed(service, config, dry_run=args.dry_run)

        if args.block_remaining:
            return _run_block_remaining(service, dry_run=args.dry_run)

        infos = fetch_message_infos(service, config.scan_days, config.max_emails)
        print(dim(f"  • Total messages fetched: {len(infos)}"))
        all_groups = group_all_by_sender(infos)
        groups = group_by_sender(infos)
        print(dim(f"  • Total unique senders: {len(all_groups)}"))
        print(dim(f"  • Unsubscribeable senders: {len(groups)}"))
        print()

        # Process unsubscribeable senders first
        summary = RunSummary()
        if groups:
            summary = run_interactive(
                service,
                groups,
                config,
                dry_run=args.dry_run,
                trash_enabled=not args.no_trash,
            )

        # Then handle senders without unsubscribe headers
        unsub_emails = {g.email.lower() for g in groups}
        non_unsub = [g for g in all_groups if g.email.lower() not in unsub_emails]
        blocked = 0
        if non_unsub:
            print()
            blocked = _block_or_skip_senders(service, non_unsub, config, dry_run=args.dry_run)

        _print_summary(summary, dry_run=args.dry_run)
        _write_log(summary, dry_run=args.dry_run)

        if non_unsub:
            print()
            print(dim(f"  Also saw {len(non_unsub)} senders without unsubscribe headers."))
            print(f"    Blocked (filter): {green(str(blocked))}")
            print(f"    Skipped:          {dim(str(len(non_unsub) - blocked))}")

        return 0
    except KeyboardInterrupt:
        print()
        print(yellow("\n  • Cancelled by user."))
        return 1


if __name__ == "__main__":
    sys.exit(main())
