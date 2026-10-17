"""Tests for the WS /bots/run endpoint (app/bot_runner.py)."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import app.bot_runner as bot_runner
import app.deriv_gateway as deriv_gateway
from app.main import app

client = TestClient(app)


def test_bot_run_requires_valid_ticket():
    """Connecting without a valid ticket must close with code 4401."""
    deriv_gateway._ws_tickets.clear()
    with pytest.raises(Exception):
        # TestClient raises on non-101 WS upgrade; close code 4401 causes an error.
        with client.websocket_connect("/bots/run?account_id=VRTC123"):
            pass


def test_bot_run_rejects_missing_account_id():
    """Connecting without account_id must close (no valid ticket check reaches account_id)."""
    deriv_gateway._ws_tickets.clear()
    # No ticket → closes 4401 before reaching account_id validation, which is fine.
    with pytest.raises(Exception):
        with client.websocket_connect("/bots/run"):
            pass


def test_bot_run_rejects_invalid_account_id():
    """An account_id with invalid characters must close with code 4400."""
    deriv_gateway._ws_tickets.clear()
    ticket = "test-ticket-invalid-acct"
    deriv_gateway._ws_tickets[ticket] = ("user-1", time.time() + 30)
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/bots/run?account_id=bad/account",
            headers={"sec-websocket-protocol": f"tradebridge, {ticket}"},
        ):
            pass


def test_bot_run_rejects_expired_ticket():
    """An expired ticket must close with code 4401."""
    deriv_gateway._ws_tickets.clear()
    ticket = "expired-ticket"
    deriv_gateway._ws_tickets[ticket] = ("user-1", time.time() - 1)
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/bots/run?account_id=VRTC123",
            headers={"sec-websocket-protocol": f"tradebridge, {ticket}"},
        ):
            pass


def test_bot_run_rejects_concurrent_run_for_same_account():
    """A second connection for the same (user, account) must close with code 4409."""
    deriv_gateway._ws_tickets.clear()
    run_key = ("user-concurrent", "VRTC999")
    bot_runner._active_runs.add(run_key)
    try:
        ticket = "concurrent-ticket"
        deriv_gateway._ws_tickets[ticket] = ("user-concurrent", time.time() + 30)
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/bots/run?account_id=VRTC999",
                headers={"sec-websocket-protocol": f"tradebridge, {ticket}"},
            ):
                pass
    finally:
        bot_runner._active_runs.discard(run_key)


def test_active_runs_cleaned_up_after_credential_failure(monkeypatch):
    """_active_runs must not leak if the connection fails before accepting."""
    deriv_gateway._ws_tickets.clear()
    ticket = "cleanup-ticket"
    deriv_gateway._ws_tickets[ticket] = ("user-cleanup", time.time() + 30)

    async def bad_credential(user_id: str):
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="Not connected")

    monkeypatch.setattr(bot_runner, "_owned_credential", bad_credential)

    run_key = ("user-cleanup", "VRTC123")
    assert run_key not in bot_runner._active_runs

    with pytest.raises(Exception):
        with client.websocket_connect(
            "/bots/run?account_id=VRTC123",
            headers={"sec-websocket-protocol": f"tradebridge, {ticket}"},
        ):
            pass

    # run_key must NOT have been added (credential failed before _active_runs.add)
    assert run_key not in bot_runner._active_runs
