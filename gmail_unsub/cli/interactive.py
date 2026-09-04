"""Interactive per-sender prompt loop."""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from ..core.classifier import check_sender
from ..core.unsubscribe import (
    UnsubscribeResult,
    describe_targets,
    execute_unsubscribe,
    parse_list_unsubscribe,
)
from ..gmail.actions import trash_messages
from ..gmail.scan import SenderGroup
from ..store.settings import Settings


# --- minimal ANSI helpers (no extra deps) ---
USE_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if not USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str: return _c("1", t)
def dim(t: str) -> str: return _c("2", t)
def red(t: str) -> str: return _c("31", t)
def green(t: str) -> str: return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def cyan(t: str) -> str: return _c("36", t)


@dataclass
class ActionRecord:
    sender: str
    method: str
    detail: str
    trashed_count: int
    dry_run: bool
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class RunSummary:
    unsubscribed: List[ActionRecord] = field(default_factory=list)
    skipped_sensitive: List[str] = field(default_factory=list)
    skipped_user: List[str] = field(default_factory=list)
    failures: List[ActionRecord] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return len(self.unsubscribed) + len(self.skipped_sensitive) + len(self.skipped_user) + len(self.failures)


def _format_date(d: datetime) -> str:
    delta = datetime.now() - d
    if delta.days == 0:
        return "today"
    if delta.days == 1:
        return "yesterday"
    if delta.days < 30:
        return f"{delta.days} days ago"
    if delta.days < 365:
        return f"{delta.days // 30} months ago"
    return f"{delta.days // 365} years ago"


def _display_sender(group: SenderGroup, idx: int, total: int) -> str:
    header = bold(f"[{idx}/{total}] ") + cyan(group.display_sender)
    meta = dim(
        f"        {group.count} email{'s' if group.count != 1 else ''} • "
        f"Last: {_format_date(group.last_date)}"
    )
    subjects = group.sample_subjects
    subj_lines = "\n".join(f"        Sample: {yellow(repr(s))}" for s in subjects) if subjects else ""
    return f"{header}\n{meta}\n{subj_lines}".rstrip()


def _prompt_choice(allow_force: bool) -> str:
    suffix = "/f force" if allow_force else ""
    prompt = f"  [y]unsub  [n]skip  [a]unsub-always  [s]skip-all  [q]quit{suffix}? "
    while True:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        try:
            choice = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "q"
        if choice in ("y", "n", "a", "s", "q", "f", ""):
            return choice or "n"
        print(f"  {dim('(invalid; choose y/n/a/s/q)')}")


def run_interactive(
    service,
    groups: List[SenderGroup],
    config: Settings,
    dry_run: bool = False,
    trash_enabled: bool = True,
) -> RunSummary:
    """Walk through each sender and ask the user to confirm."""
    summary = RunSummary()

    if not groups:
        print(green("  No senders with List-Unsubscribe headers found."))
        return summary

    total = min(len(groups), config.max_senders)
    print()
    print(bold(f"  Found {len(groups)} senders with unsubscribe links."))
    if total < len(groups):
        print(dim(f"  Showing first {total} (use --max to change)."))
    if dry_run:
        print(yellow("  [PREVIEW ONLY — no real unsubscribes sent, no emails trashed]"))
    print()

    for i in range(total):
        group = groups[i]
        if group.email.lower() in (s.lower() for s in config.always_trust_senders):
            print(_display_sender(group, i + 1, total))
            print(dim(f"  → auto-unsubscribe (saved from previous run)"))
            print()
            _do_unsubscribe(service, group, config, dry_run, trash_enabled, summary)
            time.sleep(config.rate_limit_seconds)
            continue

        sens = check_sender(group.email, group.name, config.sensitive_keywords)
        if sens.is_sensitive:
            print(_display_sender(group, i + 1, total))
            print(red(f"  ⚠ SENSITIVE — matches: {', '.join(sens.reasons)}"))
            choice = _prompt_choice(allow_force=True)
            if choice == "f":
                print(dim("  → forced through sensitivity filter"))
            elif choice in ("n", ""):
                print(dim("  → Skipped (kept in inbox)"))
                summary.skipped_sensitive.append(group.email)
                print()
                continue
            elif choice == "s":
                print(dim("  → Stopping early."))
                break
            elif choice == "q":
                print(dim("  → Quit."))
                return summary
            else:
                print(dim("  → Proceeding despite sensitivity."))
        else:
            print(_display_sender(group, i + 1, total))
            targets = parse_list_unsubscribe(group.list_unsubscribe)
            one_click = "list-unsubscribe=one-click" in (group.list_unsubscribe_post or "").lower()
            print(f"  Method: {describe_targets(targets, one_click)}")
            choice = _prompt_choice(allow_force=False)

        if choice == "s":
            print(dim("  → Stopping early."))
            break
        if choice == "q":
            print(dim("  → Quit."))
            return summary
        if choice == "n":
            print(dim("  → Skipped."))
            summary.skipped_user.append(group.email)
            print()
            continue
        if choice == "a":
            config.trust(group.email)
            print(dim(f"  → Auto-unsubscribe saved — won't ask again for {group.email}"))

        _do_unsubscribe(service, group, config, dry_run, trash_enabled, summary)
        time.sleep(config.rate_limit_seconds)
        print()

    return summary


def _record_failure(store, group: SenderGroup, failed_method: str) -> None:
    store.record_failure(
        group.email, group.name, failed_method,
        group.count, group.list_unsubscribe, group.list_unsubscribe_post,
    )


def _do_unsubscribe(
    service,
    group: SenderGroup,
    config: Settings,
    dry_run: bool,
    trash_enabled: bool,
    summary: RunSummary,
) -> None:
    targets = parse_list_unsubscribe(group.list_unsubscribe)
    one_click = "list-unsubscribe=one-click" in (group.list_unsubscribe_post or "").lower()

    if dry_run:
        summary.unsubscribed.append(
            ActionRecord(
                sender=group.email,
                method=describe_targets(targets, one_click),
                detail="dry-run (no action)",
                trashed_count=0,
                dry_run=True,
            )
        )
        config.store.log_action(
            group.email, "unsubscribe", describe_targets(targets, one_click),
            "dry-run (no action)", ok=True, dry_run=True,
        )
        print(yellow(f"  [PREVIEW] Would unsubscribe via {describe_targets(targets, one_click)}"))
        return

    result: UnsubscribeResult = execute_unsubscribe(
        service, targets, one_click, group.sample_subjects[0] if group.sample_subjects else ""
    )

    trashed = 0
    if trash_enabled:
        msg_ids = [m.id for m in group.messages]
        trashed = trash_messages(service, msg_ids)
        if trashed:
            config.store.mark_trashed(msg_ids)

    record = ActionRecord(
        sender=group.email,
        method=result.method,
        detail=result.detail,
        trashed_count=trashed,
        dry_run=False,
    )

    config.store.log_action(
        group.email, "unsubscribe", result.method, result.detail,
        ok=result.ok, trashed_count=trashed, dry_run=False,
    )

    if result.ok:
        summary.unsubscribed.append(record)
        config.store.mark_unsubscribed(group.email)
        config.store.clear_failure(group.email)
        msg = f"  ✓ Unsubscribed ({result.detail})"
        if trashed:
            msg += f" • Trashed {trashed} email{'s' if trashed != 1 else ''}"
        print(green(msg))
    else:
        summary.failures.append(record)
        _record_failure(config.store, group, result.method)
        msg = f"  ✗ Unsub failed: {result.detail}"
        if trashed:
            msg += f" • Trashed {trashed} email{'s' if trashed != 1 else ''} anyway"
        print(red(msg))
