import asyncio
import json
import os
from types import SimpleNamespace

from app.bot_worker import DurableBotWorker


def make_worker(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-test-key")
    return DurableBotWorker()


def test_uncertain_buy_ack_is_persisted_and_never_retried(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        events = []
        request_calls = []
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

        async def credential(_run):
            return SimpleNamespace(access_token="demo-token", expires_at=9_999_999_999)

        class FakeSocket:
            async def close(self):
                return None

        async def open_socket(_run, _credential):
            return FakeSocket()

        async def rows(table, _params):
            return [run] if table == "bot_runs" else []

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
        await worker.execute_run(run)
        await worker.close()
        assert request_calls == ["proposal", "buy"]
        assert ("trade", "buy_pending") in events
        assert ("trade", "outcome_unknown") in events
        assert any(event[0] == "run" and event[1] == "reconciliation_required" for event in events)

    asyncio.run(scenario())


def test_settlement_records_broker_financials_and_stops_at_trade_limit(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        events = []
        run = {
            "id": "run-2", "status": "running", "started_at": "2026-10-17T04:00:00+00:00",
            "config": {"stopConditions": [{"type": "tradeCount", "max": 1}],
                       "maxTradesPerDay": 1, "maxConsecutiveLosses": 3},
        }
        trade = {"id": "trade-2", "contract_id": "12345", "state": "open"}

        class SettledSocket:
            async def send(self, _payload):
                return None

            async def recv(self):
                return json.dumps({"proposal_open_contract": {
                    "contract_id": 12345, "is_sold": True, "status": "won",
                    "payout": 1.95, "profit": 0.95,
                }})

        async def trade_patch(_trade_id, body):
            events.append(("trade", body))
            return {"id": "trade-2", **body}

        async def rows(table, _params):
            if table == "bot_runs":
                return [{"trade_count": 0, "wins": 0, "losses": 0, "realized_pnl": 0,
                         "config": run["config"], "status": "running"}]
            return [{"profit": 0.95, "state": "settled"}]

        async def set_run(_run_id, status, **fields):
            events.append(("run", status, fields))

        async def patch_one(_table, _params, _body):
            return {"id": "run-2"}

        worker.trade_patch = trade_patch
        worker.rows = rows
        worker.set_run = set_run
        worker.patch_one = patch_one
        keep_running = await worker.monitor_contract(SettledSocket(), run, trade)
        await worker.close()
        settlement = next(body for kind, body in events if kind == "trade" and body.get("state") == "settled")
        assert settlement["payout"] == 1.95
        assert settlement["profit"] == 0.95
        assert keep_running is False
        assert any(event[0] == "run" and event[1] == "stopped" for event in events)

    asyncio.run(scenario())


def test_stale_worker_with_inflight_trade_requires_reconciliation(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        events = []

        async def rows(table, _params):
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


def test_stale_worker_with_known_contract_requeues_monitoring_not_buy(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        events = []

        async def rows(table, _params):
            if table == "bot_runs":
                return [{"id": "run-3", "status": "running", "heartbeat_at": stale}]
            return [{"id": "trade-3", "state": "outcome_unknown", "contract_id": "12345"}]

        async def set_run(run_id, status, **fields):
            events.append((run_id, status, fields.get("stop_reason")))

        worker.rows = rows
        worker.set_run = set_run
        await worker.recover_stale_runs()
        await worker.close()
        assert events and events[0][1] == "queued"
        assert "known contract" in (events[0][2] or "")

    asyncio.run(scenario())


def test_worker_requires_explicit_server_credentials(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    try:
        DurableBotWorker()
    except RuntimeError as exc:
        assert "SUPABASE_URL" in str(exc)
    else:
        raise AssertionError("worker must fail closed when environment is incomplete")
