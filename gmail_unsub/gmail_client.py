"""Gmail API wrapper: fetch, group, and trash messages."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr
from typing import Dict, Iterable, List, Optional, Tuple

METADATA_HEADERS = [
    "From",
    "Subject",
    "Date",
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
]


@dataclass
class MessageInfo:
    id: str
    from_name: str
    from_email: str
    subject: str
    date: datetime
    list_unsubscribe: str = ""
    list_unsubscribe_post: str = ""

    @property
    def display_sender(self) -> str:
        if self.from_name:
            return f"{self.from_name} <{self.from_email}>"
        return self.from_email


@dataclass
class SenderGroup:
    name: str
    email: str
    messages: List[MessageInfo] = field(default_factory=list)

    @property
    def display_sender(self) -> str:
        if self.name:
            return f"{self.name} <{self.email}>"
        return self.email

    @property
    def count(self) -> int:
        return len(self.messages)

    @property
    def last_date(self) -> datetime:
        return max(m.date for m in self.messages)

    @property
    def sample_subjects(self) -> List[str]:
        seen = set()
        out: List[str] = []
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
        for m in self.messages:
            if m.list_unsubscribe:
                return m.list_unsubscribe
        return ""

    @property
    def list_unsubscribe_post(self) -> str:
        for m in self.messages:
            if m.list_unsubscribe_post:
                return m.list_unsubscribe_post
        return ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "email": self.email,
            "count": self.count,
            "last_date": self.last_date.isoformat(),
            "sample_subjects": self.sample_subjects,
            "list_unsubscribe": self.list_unsubscribe,
            "list_unsubscribe_post": self.list_unsubscribe_post,
        }


def _parse_from_header(from_header: str) -> Tuple[str, str]:
    name, email = parseaddr(from_header or "")
    if not email:
        m = re.search(r"<([^>]+)>", from_header or "")
        if m:
            email = m.group(1).strip().lower()
        else:
            email = (from_header or "").strip().lower()
    return name.strip(), email.lower()


def _parse_date(date_header: str) -> datetime:
    from email.utils import parsedate_to_datetime
    if not date_header:
        return datetime.fromtimestamp(0)
    try:
        dt = parsedate_to_datetime(date_header)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (TypeError, ValueError):
        return datetime.fromtimestamp(0)


def _headers_to_dict(headers: List[dict]) -> Dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (headers or [])}


def fetch_message_infos(
    service,
    days: int,
    max_results: int = 2000,
) -> List[MessageInfo]:
    """Fetch metadata for messages from the last `days` days (0 = all time)."""
    from googleapiclient.errors import HttpError

    query = f"newer_than:{days}d" if days > 0 else ""
    ids: List[str] = []
    page_token: Optional[str] = None

    time_desc = f"newer_than:{days}d" if days > 0 else "all time"
    print(f"  • Listing messages ({time_desc})...", file=sys.stderr, end="")
    try:
        while True:
            try:
                kwargs = {
                    "userId": "me",
                    "q": query,
                    "maxResults": min(500, max_results - len(ids)),
                    "fields": "messages(id),nextPageToken",
                }
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = service.users().messages().list(**kwargs).execute()
            except HttpError as e:
                print(f"\n  ! List error: {e}", file=sys.stderr)
                break
            except KeyboardInterrupt:
                print("\n  • Listing cancelled. ", end="", file=sys.stderr)
                break

            new_ids = [m["id"] for m in resp.get("messages", [])]
            ids.extend(new_ids)
            print(f"\r  • Listing messages ({time_desc})... found {len(ids)}", end="", file=sys.stderr)
            page_token = resp.get("nextPageToken")
            if not page_token or len(ids) >= max_results:
                break
    finally:
        print(file=sys.stderr)

    ids = ids[:max_results]
    total_ids = len(ids)
    print(f"  • Fetching metadata for {total_ids} messages...", file=sys.stderr)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time
    import requests as req

    def _fetch_one(msg_id: str) -> Optional[MessageInfo]:
        base_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}"
        from urllib.parse import urlencode
        flat_params = [("format", "metadata"), ("fields", "id,payload/headers")]
        for h in METADATA_HEADERS:
            flat_params.append(("metadataHeaders", h))
        url = f"{base_url}?{urlencode(flat_params)}"

        creds_obj = service._http.credentials if hasattr(service._http, 'credentials') else None
        if creds_obj is None:
            creds_obj = getattr(service, '_credentials', None)
        token = None
        if creds_obj is not None:
            token = getattr(creds_obj, 'token', None)

        for attempt in range(3):
            try:
                resp = req.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if resp.status_code == 401 and attempt < 2:
                    if creds_obj and hasattr(creds_obj, 'refresh'):
                        from google.auth.transport.requests import Request
                        creds_obj.refresh(Request())
                        token = creds_obj.token
                    time.sleep(0.5)
                    continue
                if resp.status_code == 429 and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                resp.raise_for_status()
                msg = resp.json()

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
                    list_unsubscribe=hdrs.get("list-unsubscribe", ""),
                    list_unsubscribe_post=hdrs.get("list-unsubscribe-post", ""),
                )
            except KeyboardInterrupt:
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                print(f"  ! Get error for {msg_id}: {e}", file=sys.stderr)
                return None
        return None

    infos: List[MessageInfo] = []
    completed = 0
    ex = ThreadPoolExecutor(max_workers=2)
    try:
        futures = {ex.submit(_fetch_one, mid): mid for mid in ids}
        for fut in as_completed(futures):
            try:
                result = fut.result(timeout=0.1)
            except KeyboardInterrupt:
                print("\n  • Metadata fetch cancelled.", file=sys.stderr)
                break
            if result is not None:
                infos.append(result)
            completed += 1
            if completed % 50 == 0 or completed == total_ids:
                print(f"\r  • Fetching metadata... {completed}/{total_ids}", end="", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n  • Metadata fetch cancelled.", file=sys.stderr)
    finally:
        ex.shutdown(wait=False)
    print(file=sys.stderr)

    return infos


def group_by_sender(infos: Iterable[MessageInfo]) -> List[SenderGroup]:
    """Group messages by sender email and sort by count descending.
    Only returns senders with List-Unsubscribe headers."""
    groups: Dict[str, SenderGroup] = {}
    for info in infos:
        if not info.list_unsubscribe:
            continue
        if info.from_email not in groups:
            groups[info.from_email] = SenderGroup(
                name=info.from_name, email=info.from_email
            )
        groups[info.from_email].messages.append(info)
        if not groups[info.from_email].name and info.from_name:
            groups[info.from_email].name = info.from_name

    out = list(groups.values())
    out.sort(key=lambda g: g.count, reverse=True)
    return out


def group_all_by_sender(infos: Iterable[MessageInfo]) -> List[SenderGroup]:
    """Group ALL messages by sender (including those without unsubscribe headers)."""
    groups: Dict[str, SenderGroup] = {}
    for info in infos:
        if info.from_email not in groups:
            groups[info.from_email] = SenderGroup(
                name=info.from_name, email=info.from_email
            )
        groups[info.from_email].messages.append(info)
        if not groups[info.from_email].name and info.from_name:
            groups[info.from_email].name = info.from_name

    out = list(groups.values())
    out.sort(key=lambda g: g.count, reverse=True)
    return out


def trash_messages(service, message_ids: List[str]) -> int:
    """Move messages to trash (INBOX removed, TRASH added).

    Gmail auto-deletes trash after 30 days, so this is recoverable.
    """
    from googleapiclient.errors import HttpError

    if not message_ids:
        return 0
    total = 0
    BATCH = 1000
    for i in range(0, len(message_ids), BATCH):
        chunk = message_ids[i : i + BATCH]
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


def find_all_from_sender(service, sender_email: str) -> List[str]:
    """Return every message id (all time) from a given sender address."""
    from googleapiclient.errors import HttpError

    ids: List[str] = []
    page_token: Optional[str] = None
    query = f"from:{sender_email}"

    while True:
        try:
            kwargs = {
                "userId": "me",
                "q": query,
                "maxResults": 500,
                "fields": "messages(id),nextPageToken",
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.users().messages().list(**kwargs).execute()
        except HttpError as e:
            print(f"  ! Search error for {sender_email}: {e}", file=sys.stderr)
            break

        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return ids


def refetch_sender_headers(service, sender_email: str) -> List[dict]:
    """Fetch the most recent messages from a sender to get fresh unsubscribe headers."""
    msg_ids = find_all_from_sender(service, sender_email)
    if not msg_ids:
        return []

    results: List[dict] = []
    from googleapiclient.errors import HttpError

    for mid in msg_ids[:10]:
        try:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=METADATA_HEADERS,
                    fields="id,payload/headers",
                )
                .execute()
            )
            hdrs = _headers_to_dict(msg.get("payload", {}).get("headers", []))
            results.append({
                "id": msg["id"],
                "list_unsubscribe": hdrs.get("list-unsubscribe", ""),
                "list_unsubscribe_post": hdrs.get("list-unsubscribe-post", ""),
                "subject": hdrs.get("subject", ""),
            })
        except HttpError:
            continue
        if len(results) >= 3:
            break
    return results


def create_auto_trash_filter(service, sender_email: str) -> bool:
    """Create a permanent Gmail filter that auto-trashes emails from a sender."""
    from googleapiclient.errors import HttpError

    body = {
        "criteria": {"from": sender_email},
        "action": {
            "removeLabelIds": ["INBOX"],
            "addLabelIds": ["TRASH"],
        },
    }
    try:
        service.users().settings().filters().create(userId="me", body=body).execute()
        return True
    except HttpError as e:
        detail = str(e)
        if "already exists" in detail.lower() or "duplicate" in detail.lower():
            return True  # filter already exists, that's fine
        print(f"  ! Filter create error for {sender_email}: {e}", file=sys.stderr)
        return False
