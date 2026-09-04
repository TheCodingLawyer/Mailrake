"""Local HTTP server backing the browser control panel.

Binds loopback only, on an ephemeral port, behind a per-session token. See
security.py for why both of those matter.

The server is a thin shell: every decision it makes is delegated to the same
core the CLI uses, so the two front-ends cannot drift apart in behaviour.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..core.classifier import check_sender
from ..core.unsubscribe import (
    describe_targets,
    execute_unsubscribe,
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
from ..gmail.auth import SCOPES, authenticate, build_gmail_service
from ..gmail.scan import groups_from_cache, scan_async
from ..store.db import Store
from ..store.settings import Settings
from .security import SESSION_TOKEN, guard

STATIC_DIR = Path(__file__).parent / "static"


# --- server-side session state ------------------------------------------


@dataclass
class AppState:
    store: Store
    settings: Settings
    creds: Any = None
    service: Any = None
    groups: list = field(default_factory=list)
    scanning: bool = False
    scan_task: Any = None          # the in-flight asyncio.Task, for cancellation
    last_scan: dict = field(default_factory=dict)
    progress: dict = field(default_factory=lambda: {"phase": "idle", "done": 0, "total": 0})
    listeners: list[asyncio.Queue] = field(default_factory=list)

    def publish(self, event: dict) -> None:
        """Fan an event out to every connected SSE listener."""
        for q in list(self.listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def group_for(self, email: str):
        return next((g for g in self.groups if g.email == email), None)


STATE: AppState | None = None


# --- request models ------------------------------------------------------


class ScanRequest(BaseModel):
    days: int | None = None
    max_emails: int | None = None
    bulk_only: bool | None = None
    fresh: bool = False


class SenderAction(BaseModel):
    emails: list[str]
    trash: bool = True
    dry_run: bool = False
    # Sensitive senders are refused unless the caller says so explicitly.
    # The UI only sets this after a distinct second confirmation.
    force_sensitive: bool = False


# --- app -----------------------------------------------------------------


def create_app(state: AppState) -> FastAPI:
    global STATE
    STATE = state
    app = FastAPI(title="mailrake", docs_url=None, redoc_url=None)
    api = [Depends(guard)]

    # --- read ------------------------------------------------------------

    @app.get("/api/session", dependencies=api)
    async def session():
        totals = state.store.totals()
        return {
            "authenticated": state.creds is not None,
            "scanning": state.scanning,
            "progress": state.progress,
            "cached": totals,
            "cached_ids": len(state.store.known_message_ids()),
            "last_scan": state.last_scan,
            "trusted": len(state.store.trusted_senders()),
            "failures": len(state.store.failures()),
            "settings": {
                "days": state.settings.scan_days,
                "max_emails": state.settings.max_emails,
                "bulk_only": state.settings.bulk_only,
            },
        }

    @app.get("/api/senders", dependencies=api)
    async def senders():
        """Scanned senders, annotated for the review list."""
        out = []
        for g in state.groups:
            targets = parse_list_unsubscribe(g.list_unsubscribe)
            one_click = "list-unsubscribe=one-click" in (
                g.list_unsubscribe_post or "").lower()
            sens = check_sender(g.email, g.name, state.settings.sensitive_keywords)
            out.append({
                **g.to_dict(),
                "method": describe_targets(targets, one_click),
                "can_unsubscribe": bool(targets),
                "sensitive": sens.is_sensitive,
                "sensitive_reasons": sens.reasons,
                "trusted": state.store.is_trusted(g.email),
            })
        return {"senders": out}

    @app.get("/api/storage", dependencies=api)
    async def storage():
        return {
            "totals": state.store.totals(),
            "by_sender": [dict(r) for r in state.store.storage_by_sender(100)],
            "largest": [dict(r) for r in state.store.largest_messages(50)],
        }

    @app.get("/api/history", dependencies=api)
    async def history():
        return {
            "actions": [dict(r) for r in state.store.recent_actions(200)],
            "failures": [dict(r) for r in state.store.failures()],
        }

    # --- scan ------------------------------------------------------------

    @app.post("/api/scan", dependencies=api)
    async def start_scan(req: ScanRequest):
        if state.scanning:
            raise HTTPException(409, "A scan is already running")
        state.scanning = True          # set here so a fast second call still 409s
        state.scan_task = asyncio.create_task(_run_scan(state, req))
        return {"started": True, "fresh": req.fresh}

    @app.post("/api/scan/cancel", dependencies=api)
    async def cancel_scan():
        """Stop an in-flight scan. Whatever was fetched is already saved."""
        task = state.scan_task
        if task is None or task.done():
            return {"cancelled": False, "reason": "no scan running"}
        task.cancel()
        return {"cancelled": True}

    @app.get("/api/events", dependencies=api)
    async def events():
        """Server-sent scan progress, so a long first scan feels alive."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        state.listeners.append(queue)

        async def stream():
            try:
                yield f"data: {json.dumps({'type': 'progress', **state.progress})}\n\n"
                while True:
                    event = await queue.get()
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                if queue in state.listeners:
                    state.listeners.remove(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # --- write -----------------------------------------------------------

    @app.post("/api/unsubscribe", dependencies=api)
    async def unsubscribe(req: SenderAction):
        return await asyncio.to_thread(_do_unsubscribe, state, req)

    @app.post("/api/trust", dependencies=api)
    async def trust(req: SenderAction):
        for email in req.emails:
            g = state.group_for(email)
            state.store.trust(email, g.name if g else "")
        return {"trusted": state.store.trusted_senders()}

    @app.post("/api/block", dependencies=api)
    async def block(req: SenderAction):
        return await asyncio.to_thread(_do_block, state, req)

    @app.post("/api/retry-failed", dependencies=api)
    async def retry_failed(req: SenderAction):
        return await asyncio.to_thread(_do_retry, state, req)

    @app.post("/api/cleanup", dependencies=api)
    async def cleanup(req: SenderAction):
        return await asyncio.to_thread(_do_cleanup, state, req)

    # --- static ----------------------------------------------------------

    if STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"
                                         if (STATIC_DIR / "assets").exists()
                                         else STATIC_DIR), name="assets")

    @app.get("/")
    async def index():
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            return JSONResponse(
                {"error": "UI not built. Run `npm --prefix web run build`."},
                status_code=503,
            )
        return FileResponse(index_file)

    return app


# --- work, shared with the CLI's semantics -------------------------------


async def _run_scan(state: AppState, req: ScanRequest) -> None:
    days = req.days if req.days is not None else state.settings.scan_days
    max_emails = req.max_emails or state.settings.max_emails
    bulk_only = state.settings.bulk_only if req.bulk_only is None else req.bulk_only

    def progress(phase: str, done: int, total: int) -> None:
        state.progress = {"phase": phase, "done": done, "total": total}
        state.publish({"type": "progress", "phase": phase, "done": done, "total": total})

    try:
        skip = set() if req.fresh else state.store.known_message_ids()

        # Persist as results arrive. The server dies with its terminal, and a
        # long scan must not evaporate when that happens.
        def save(batch):
            state.store.save_scanned(batch)

        result, history_id = await scan_async(
            state.creds, days=days, max_emails=max_emails,
            bulk_only=bulk_only, skip_ids=skip, progress=progress,
            on_batch=save,
        )

        if history_id:
            state.store.set_meta("history_id", history_id)

        # Build from the full cache, not just this scan's slice. Otherwise a
        # rescan that correctly fetches nothing new empties the whole list.
        state.groups = groups_from_cache(state.store, unsubscribable_only=True)
        state.last_scan = {"fetched": len(result.messages),
                           "dropped": len(result.failed_ids),
                           "cancelled": False}
        state.publish({
            "type": "done",
            "fetched": len(result.messages),
            "dropped": len(result.failed_ids),
            "senders": len(state.groups),
        })
    except asyncio.CancelledError:
        # Partial results were already written to the cache as they arrived.
        state.groups = groups_from_cache(state.store, unsubscribable_only=True)
        state.last_scan = {"cancelled": True}
        state.publish({"type": "cancelled", "senders": len(state.groups),
                       "cached": state.store.totals()["messages"]})
    except Exception as e:  # surface, never swallow
        state.groups = groups_from_cache(state.store, unsubscribable_only=True)
        state.publish({"type": "error", "message": str(e)})
    finally:
        state.scanning = False
        state.scan_task = None
        state.progress = {"phase": "idle", "done": 0, "total": 0}


def _do_unsubscribe(state: AppState, req: SenderAction) -> dict:
    results = []
    for email in req.emails:
        group = state.group_for(email)
        if group is None:
            results.append({"email": email, "ok": False, "detail": "not in current scan"})
            continue

        sens = check_sender(email, group.name, state.settings.sensitive_keywords)
        if sens.is_sensitive and not req.force_sensitive:
            results.append({"email": email, "ok": False, "skipped": "sensitive",
                            "detail": f"matches {', '.join(sens.reasons)}"})
            continue

        targets = parse_list_unsubscribe(group.list_unsubscribe)
        one_click = "list-unsubscribe=one-click" in (
            group.list_unsubscribe_post or "").lower()

        if req.dry_run:
            state.store.log_action(email, "unsubscribe",
                                   describe_targets(targets, one_click),
                                   "dry-run (no action)", ok=True, dry_run=True)
            results.append({"email": email, "ok": True, "dry_run": True,
                            "detail": f"would use {describe_targets(targets, one_click)}"})
            continue

        result = execute_unsubscribe(
            state.service, targets, one_click,
            group.sample_subjects[0] if group.sample_subjects else "")

        trashed = 0
        if req.trash:
            ids = [m.id for m in group.messages]
            trashed = trash_messages(state.service, ids)
            if trashed:
                state.store.mark_trashed(ids)

        state.store.log_action(email, "unsubscribe", result.method, result.detail,
                               ok=result.ok, trashed_count=trashed)
        if result.ok:
            state.store.mark_unsubscribed(email)
            state.store.clear_failure(email)
        else:
            state.store.record_failure(email, group.name, result.method, group.count,
                                       group.list_unsubscribe,
                                       group.list_unsubscribe_post)

        results.append({"email": email, "ok": result.ok, "detail": result.detail,
                        "trashed": trashed})
    return {"results": results}


def _do_block(state: AppState, req: SenderAction) -> dict:
    results = []
    for email in req.emails:
        group = state.group_for(email)
        ids = [m.id for m in group.messages] if group else []
        if req.dry_run:
            results.append({"email": email, "ok": True, "dry_run": True})
            continue
        ok = block_sender(state.service, email, ids)
        if ok:
            state.store.mark_blocked(email)
            state.store.mark_trashed(ids)
        state.store.log_action(email, "block", ok=ok, trashed_count=len(ids) if ok else 0)
        results.append({"email": email, "ok": ok})
    return {"results": results}


def _do_retry(state: AppState, req: SenderAction) -> dict:
    results = []
    targets_wanted = set(req.emails) if req.emails else None
    for entry in state.store.failures():
        email = entry["email"]
        if targets_wanted and email not in targets_wanted:
            continue

        targets = parse_list_unsubscribe(entry["list_unsubscribe"])
        post = entry["list_unsubscribe_post"]
        if not targets:
            fresh = refetch_sender_headers(state.service, email)
            if fresh:
                targets = parse_list_unsubscribe(fresh[0]["list_unsubscribe"])
                post = fresh[0]["list_unsubscribe_post"]
        one_click = "list-unsubscribe=one-click" in (post or "").lower()

        if not targets:
            results.append({"email": email, "ok": False, "detail": "no target found"})
            continue

        result = retry_unsubscribe(state.service, targets, one_click,
                                   entry["failed_method"])
        state.store.log_action(email, "retry", result.method, result.detail, ok=result.ok)
        if result.ok:
            state.store.clear_failure(email)
            state.store.mark_unsubscribed(email)
        results.append({"email": email, "ok": result.ok, "detail": result.detail})
    return {"results": results}


def _do_cleanup(state: AppState, req: SenderAction) -> dict:
    emails = req.emails or state.store.trusted_senders()
    results = []
    for email in emails:
        ids = find_all_from_sender(state.service, email)
        if req.dry_run:
            results.append({"email": email, "would_trash": len(ids)})
            continue
        count = trash_messages(state.service, ids)
        state.store.mark_trashed(ids)
        state.store.log_action(email, "cleanup", trashed_count=count, ok=True)
        results.append({"email": email, "trashed": count})
    return {"results": results}


# --- launcher ------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(store: Store, settings: Settings, open_browser: bool = True) -> int:
    """Authenticate, then run the control panel on loopback."""
    import uvicorn

    print("  • Authenticating with Google...")
    creds = authenticate(SCOPES)
    service = build_gmail_service(creds)
    print("  ✓ Authenticated.\n")

    state = AppState(store=store, settings=settings, creds=creds, service=service)
    # Show what is already cached immediately, without requiring a scan first.
    state.groups = groups_from_cache(store, unsubscribable_only=True)
    if state.groups:
        print(f"  {len(state.groups)} senders loaded from cache.")
    app = create_app(state)

    port = _free_port()
    url = f"http://127.0.0.1:{port}/?token={SESSION_TOKEN}"

    print(f"  Control panel: {url}")
    print("  This link contains your session token. It works only on this")
    print("  machine, and only until you close the server.\n")
    print("  Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
