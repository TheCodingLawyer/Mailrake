"""The localhost server is an attack surface. These tests are the guardrail.

A page on any origin the user has open can fire requests at 127.0.0.1, and
DNS rebinding can point a hostile name there. Both must fail closed.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mailrake.server.app import AppState, create_app
from mailrake.server.security import SESSION_TOKEN
from mailrake.store.db import Store
from mailrake.store.settings import Settings


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "t.db")
    app = create_app(AppState(store=store, settings=Settings.load(store)))
    # base_url matters: the server rejects any Host that is not loopback,
    # and TestClient defaults to http://testserver.
    with TestClient(app, base_url="http://127.0.0.1:8123") as c:
        yield c
    store.close()


def test_valid_token_is_accepted(client):
    r = client.get("/api/session", headers={"X-Session-Token": SESSION_TOKEN})
    assert r.status_code == 200


def test_token_also_accepted_in_query_for_eventsource(client):
    # EventSource cannot set headers, so SSE has to pass it this way.
    r = client.get(f"/api/session?token={SESSION_TOKEN}")
    assert r.status_code == 200


def test_missing_token_is_rejected(client):
    assert client.get("/api/session").status_code == 401


def test_wrong_token_is_rejected(client):
    r = client.get("/api/session", headers={"X-Session-Token": "not-the-token"})
    assert r.status_code == 401


@pytest.mark.parametrize("origin", [
    "http://evil.example",
    "https://gmail.com",
    "http://127.0.0.1.evil.example",   # suffix trick
    "null",
])
def test_cross_origin_requests_are_refused(client, origin):
    r = client.get("/api/session",
                   headers={"X-Session-Token": SESSION_TOKEN, "Origin": origin})
    assert r.status_code == 403


def test_rebound_host_header_is_refused(client):
    """DNS rebinding: hostile name resolving to 127.0.0.1."""
    r = client.get("/api/session",
                   headers={"X-Session-Token": SESSION_TOKEN, "Host": "evil.example"})
    assert r.status_code == 403


def test_loopback_origin_is_allowed(client):
    r = client.get("/api/session",
                   headers={"X-Session-Token": SESSION_TOKEN,
                            "Origin": "http://127.0.0.1:8123"})
    assert r.status_code == 200


def test_destructive_endpoints_are_all_guarded(client):
    """No mutation route may be reachable without the token."""
    for path in ("/api/unsubscribe", "/api/block", "/api/cleanup",
                 "/api/retry-failed", "/api/trust", "/api/scan"):
        r = client.post(path, json={"emails": ["x@example.com"]})
        assert r.status_code == 401, f"{path} was reachable without a token"


def test_api_refuses_sensitive_senders_without_explicit_force(tmp_path):
    """The sensitivity guard must hold at the API boundary, not just the UI.

    A bug here would let a mis-click unsubscribe someone from their bank.
    """
    from datetime import datetime

    from mailrake.gmail.scan import MessageInfo, group_by_sender

    store = Store(tmp_path / "s.db")
    state = AppState(store=store, settings=Settings.load(store))
    state.groups = group_by_sender([
        MessageInfo(id="1", from_name="Google Accounts",
                    from_email="no-reply@accounts.google.com",
                    subject="Security alert", date=datetime(2026, 1, 1),
                    list_unsubscribe="<https://ex.com/u>"),
        MessageInfo(id="2", from_name="Deals", from_email="deals@shop.example",
                    subject="Sale", date=datetime(2026, 1, 1),
                    list_unsubscribe="<https://ex.com/u>"),
    ], unsubscribable_only=True)

    app = create_app(state)
    with TestClient(app, base_url="http://127.0.0.1:8123") as c:
        headers = {"X-Session-Token": SESSION_TOKEN}
        body = {"emails": ["no-reply@accounts.google.com", "deals@shop.example"],
                "dry_run": True, "trash": False}

        results = c.post("/api/unsubscribe", json=body, headers=headers).json()["results"]
        by_email = {r["email"]: r for r in results}
        assert by_email["no-reply@accounts.google.com"]["skipped"] == "sensitive"
        assert by_email["deals@shop.example"]["ok"] is True

        # ...and proceeds only when the caller opts in explicitly.
        forced = c.post("/api/unsubscribe", json={**body, "force_sensitive": True},
                        headers=headers).json()["results"]
        assert all(r["ok"] for r in forced)
    store.close()
