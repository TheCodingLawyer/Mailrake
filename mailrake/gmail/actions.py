"""Mailbox mutations: trash, spam, filters, and per-sender lookups.

These stay on the official discovery client rather than raw HTTP. They are
low-volume (one call per sender, not one per message) so the client's
overhead is irrelevant, and batchModify already moves 1,000 ids per call.

Nothing here deletes permanently. `gmail.modify` cannot bypass the trash,
which is a deliberate property of the tool, not an accident of the scope.
"""
from __future__ import annotations

import sys
from typing import Sequence

from .scan import METADATA_HEADERS, _headers_to_dict

BATCH_LIMIT = 1000


def trash_messages(service, message_ids: Sequence[str]) -> int:
    """Move messages to trash. Gmail purges trash after 30 days, so this
    stays recoverable for a month."""
    from googleapiclient.errors import HttpError

    if not message_ids:
        return 0

    total = 0
    for i in range(0, len(message_ids), BATCH_LIMIT):
        chunk = list(message_ids[i : i + BATCH_LIMIT])
        try:
            service.users().messages().batchModify(
                userId="me",
                body={
                    "ids": chunk,
                    "removeLabelIds": ["INBOX"],
                    "addLabelIds": ["TRASH"],
                },
            ).execute()
            total += len(chunk)
        except HttpError as e:
            print(f"  ! Trash error: {e}", file=sys.stderr)
    return total


def mark_spam(service, message_ids: Sequence[str]) -> bool:
    from googleapiclient.errors import HttpError

    for i in range(0, len(message_ids), BATCH_LIMIT):
        try:
            service.users().messages().batchModify(
                userId="me",
                body={"ids": list(message_ids[i : i + BATCH_LIMIT]),
                      "addLabelIds": ["SPAM"]},
            ).execute()
        except HttpError as e:
            print(f"  ! Spam mark error: {e}", file=sys.stderr)
            return False
    return True


def create_auto_trash_filter(service, sender_email: str) -> bool:
    """Permanent Gmail filter that auto-trashes future mail from a sender.

    This is the fallback for senders whose unsubscribe endpoint is dead or
    lying. Requires the gmail.settings.basic scope.
    """
    from googleapiclient.errors import HttpError

    try:
        service.users().settings().filters().create(
            userId="me",
            body={
                "criteria": {"from": sender_email},
                "action": {"removeLabelIds": ["INBOX"], "addLabelIds": ["TRASH"]},
            },
        ).execute()
        return True
    except HttpError as e:
        detail = str(e).lower()
        if "already exists" in detail or "duplicate" in detail:
            return True  # idempotent by intent
        print(f"  ! Filter create error for {sender_email}: {e}", file=sys.stderr)
        return False


def block_sender(service, sender_email: str, message_ids: Sequence[str]) -> bool:
    """Gmail's 'block' equivalent: existing mail to spam, future mail filtered."""
    if message_ids and not mark_spam(service, message_ids):
        return False
    return create_auto_trash_filter(service, sender_email)


def find_all_from_sender(service, sender_email: str) -> list[str]:
    """Every message id from a sender, all time — used by cleanup."""
    from googleapiclient.errors import HttpError

    ids: list[str] = []
    page_token: str | None = None

    while True:
        kwargs = {
            "userId": "me",
            "q": f"from:{sender_email}",
            "maxResults": 500,
            "fields": "messages(id),nextPageToken",
        }
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            resp = service.users().messages().list(**kwargs).execute()
        except HttpError as e:
            print(f"  ! Search error for {sender_email}: {e}", file=sys.stderr)
            break

        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return ids


def refetch_sender_headers(service, sender_email: str, limit: int = 3) -> list[dict]:
    """Pull fresh unsubscribe headers for a sender.

    Unsubscribe URLs are often single-use or time-limited, so a stale one
    from a previous run is a common cause of retry failure.
    """
    from googleapiclient.errors import HttpError

    msg_ids = find_all_from_sender(service, sender_email)
    results: list[dict] = []

    for mid in msg_ids[:10]:
        try:
            msg = (
                service.users().messages()
                .get(userId="me", id=mid, format="metadata",
                     metadataHeaders=METADATA_HEADERS, fields="id,payload/headers")
                .execute()
            )
        except HttpError:
            continue

        hdrs = _headers_to_dict(msg.get("payload", {}).get("headers", []))
        results.append({
            "id": msg["id"],
            "list_unsubscribe": hdrs.get("list-unsubscribe", ""),
            "list_unsubscribe_post": hdrs.get("list-unsubscribe-post", ""),
            "subject": hdrs.get("subject", ""),
        })
        if len(results) >= limit:
            break

    return results
