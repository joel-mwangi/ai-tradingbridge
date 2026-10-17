import asyncio
import json
import os
from types import SimpleNamespace

from app.bot_worker import DurableBotWorker, WorkerError


def make_worker(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-test-key")
    monkeypatch.setenv("DERIV_APP_ID", "12345")
    worker = DurableBotWorker()
    return worker


def test_uncertain_buy_ack_is_persisted_and_never_retried(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        events = []
        request_calls = []

        async def credential(_run):
            return SimpleNamespace(access_token="demo-token", expires_at=9_999_999_999)

        class FakeSocket:
            async def close(self):
                return None

        async def open_socket(_token):
            return FakeSocket()

        async def rows(table, _params):
            if table == "bot_runs":
                return [run]
            return []

        async def create_trade(_run):
            return {"id": "trade-1", "state": "created"}

        async def trade_patch(_trade_id, body):
            events.append(("trade", body["state"]))
            return {"id": "trade-1", **body}

        async def set_run(_run_id, status, **fields):
            events.append(("run", status, fields.get("stop_reason")))

        async def request(_ws, payload, timeout=12):
            request_calls.append(next(iter(payload)))
            if "proposal" in payload:
                return {"proposal": {"id": "proposal-1", "ask_price": 1.0}}
            if "buy" in payload:
                raise TimeoutError("ack lost")
            raise AssertionError("unexpected request")

        worker.demo_credential = credential
        worker.open_socket = open_socket
        worker.rows = rows
        worker.create_trade = create_trade
        worker.trade_patch = trade_patch
        worker.set_run = set_run
        worker.request = request
        run = {
            "id": "run-1", "user_id": "user-1", "account_id": "VRTC123",
            "currency": "USD", "config": {
                "id": "bot-1", "name": "Demo", "symbol": "1HZ100V",
                "contractType": "DIGITEVEN", "duration": 1, "durationUnit": "t",
                "stakeStrategy": {"type": "fixed", "amount": 1},
                "stopConditions": [{"type": "tradeCount", "max": 1}],
                "cooldownSeconds": 1, "maxTradesPerDay": 1,
            },
        }
        await worker.execute_run(run)
        await worker.close()
        assert request_calls == ["proposal", "buy"]
        assert ("trade", "buy_pending") in events
        assert ("trade", "outcome_unknown") in events
        assert any(event[0] == "run" and event[1] == "reconciliation_required" for event in events)

    asyncio.run(scenario())


def test_stale_worker_with_inflight_trade_requires_reconciliation(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        events = []

        async def rows(table, params):
            if table == "bot_runs":
                return [{"id": "run-1", "status": "running", "heartbeat_at": stale}]
            return [{"id": "trade-1", "state": "buy_pending", "contract_id": None}]

        async def set_run(run_id, status, **fields):
            events.append((run_id, status, fields.get("stop_reason")))

        worker.rows = rows
        worker.set_run = set_run
        await worker.recover_stale_runs()
        await worker.close()
        assert events and events[0][1] == "reconciliation_required"
        assert "manual reconciliation" in (events[0][2] or "")

    asyncio.run(scenario())


def test_worker_requires_explicit_server_credentials(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("DERIV_APP_ID", raising=False)
    try:
        DurableBotWorker()
    except RuntimeError as exc:
        assert "DERIV_APP_ID" in str(exc)
    else:
        raise AssertionError("worker must fail closed when environment is incomplete")
