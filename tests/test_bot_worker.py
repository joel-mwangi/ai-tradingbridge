import asyncio
import json
import os
import time
from types import SimpleNamespace

from app.bot_worker import DurableBotWorker


def make_worker(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-test-key")
    worker = DurableBotWorker()
    worker.last_heartbeat = time.time()
    return worker


def test_uncertain_buy_ack_is_persisted_and_never_retried(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        events = []
        request_calls = []
        run = {
            "id": "run-1", "user_id": "user-1", "account_id": "VRTC123",
            "status": "running", "currency": "USD", "config": {
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

        async def sync_run_totals(_run_id):
            return {"id": run["id"], "trade_count": 1, "wins": 1, "losses": 0,
                    "realized_pnl": 0.95, "config": run["config"], "status": "running",
                    "started_at": run["started_at"]}

        async def risk_stop_reason(_latest):
            return "Configured trade-count limit reached."

        worker.trade_patch = trade_patch
        worker.rows = rows
        worker.set_run = set_run
        worker.patch_one = patch_one
        worker.sync_run_totals = sync_run_totals
        worker.risk_stop_reason = risk_stop_reason
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

        async def trade_patch(trade_id, body):
            events.append(("trade", body.get("state"), body.get("error_message")))
            return {"id": trade_id, **body}

        worker.rows = rows
        worker.set_run = set_run
        worker.trade_patch = trade_patch
        await worker.recover_stale_runs()
        await worker.close()
        assert any(event[0] == "trade" and event[1] == "outcome_unknown" for event in events)
        assert any(event[0] != "trade" and event[1] == "reconciliation_required" for event in events)
        assert any("manual reconciliation" in (event[2] or "") for event in events if event[0] != "trade")

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


def test_known_contract_disconnect_requeues_monitoring_without_new_buy(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        events = []
        run = {"id": "run-known", "status": "running", "config": {"stopConditions": []}}
        trade = {"id": "trade-known", "contract_id": "12345", "state": "open", "monitor_attempts": 0}

        class DisconnectedSocket:
            async def send(self, _payload):
                return None

            async def recv(self):
                raise asyncio.TimeoutError()

        async def trade_patch(trade_id, body):
            events.append(("trade", body.get("state"), body.get("monitor_attempts")))
            return {"id": trade_id, **body}

        async def set_run(run_id, status, **fields):
            events.append(("run", status, fields.get("stop_reason")))

        async def rows(table, _params):
            return [{"status": "running"}] if table == "bot_runs" else []

        async def patch_one(_table, _params, _body):
            return {"id": "run-known"}

        worker.trade_patch = trade_patch
        worker.set_run = set_run
        worker.rows = rows
        worker.patch_one = patch_one
        keep_running = await worker.monitor_contract(DisconnectedSocket(), run, trade)
        await worker.close()
        assert keep_running is False
        assert ("trade", "outcome_unknown", 1) in events
        assert any(event[0] == "run" and event[1] == "queued" for event in events)
        assert not any(event[0] == "run" and event[1] == "reconciliation_required" for event in events)

    asyncio.run(scenario())


def test_run_totals_are_rebuilt_from_settled_ledger_idempotently(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        writes = []

        async def rows(table, _params):
            assert table == "bot_trades"
            return [
                {"profit": "0.95", "state": "settled"},
                {"profit": "-1.00", "state": "settled"},
                {"profit": "0.00", "state": "settled"},
            ]

        async def patch_one(table, _params, body):
            writes.append(body)
            return {"id": "run-rebuild", **body, "config": {}, "status": "running"}

        worker.rows = rows
        worker.patch_one = patch_one
        first = await worker.sync_run_totals("run-rebuild")
        second = await worker.sync_run_totals("run-rebuild")
        await worker.close()

        assert first["trade_count"] == second["trade_count"] == 3
        assert first["wins"] == second["wins"] == 1
        assert first["losses"] == second["losses"] == 2
        assert round(float(first["realized_pnl"]), 2) == round(float(second["realized_pnl"]), 2) == -0.05
        assert len(writes) == 2
        assert writes[0]["trade_count"] == writes[1]["trade_count"] == 3

    asyncio.run(scenario())


def test_restart_after_settlement_reconciles_totals_before_resuming(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        events = []

        async def rows(table, _params):
            if table == "bot_runs":
                return [{"id": "run-settled", "status": "running", "heartbeat_at": stale}]
            return [{"id": "trade-settled", "state": "settled", "contract_id": "12345"}]

        async def sync_run_totals(run_id):
            events.append(("sync", run_id))
            return {"id": run_id, "status": "running", "trade_count": 1, "wins": 1,
                    "losses": 0, "realized_pnl": 0.95, "config": {"stopConditions": []}}

        async def risk_stop_reason(_run):
            return None

        async def set_run(run_id, status, **fields):
            events.append(("run", status, fields.get("stop_reason")))

        worker.rows = rows
        worker.sync_run_totals = sync_run_totals
        worker.risk_stop_reason = risk_stop_reason
        worker.set_run = set_run
        await worker.recover_stale_runs()
        await worker.close()

        assert ("sync", "run-settled") in events
        assert any(event[0] == "run" and event[1] == "queued" for event in events)
        assert any("reconciled run counters" in (event[2] or "") for event in events if event[0] == "run")

    asyncio.run(scenario())


def test_supabase_service_role_headers_are_not_defaulted_on_upstream_http_client(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        await worker.close()
        assert "apikey" not in worker.http.headers
        assert "Authorization" not in worker.http.headers

    asyncio.run(scenario())


def test_worker_restart_preserves_stop_request_with_open_contract(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        events = []

        async def rows(table, _params):
            if table == "bot_runs":
                return [{"id": "run-stop", "status": "stopping", "heartbeat_at": stale}]
            return [{"id": "trade-open", "state": "open", "contract_id": "12345"}]

        async def set_run(run_id, status, **fields):
            events.append((run_id, status, fields.get("stop_reason")))

        worker.rows = rows
        worker.set_run = set_run
        await worker.recover_stale_runs()
        await worker.close()

        assert events and events[0][1] == "reconciliation_required"
        assert "stop request" in (events[0][2] or "").lower()

    asyncio.run(scenario())



def test_stale_worker_heartbeat_blocks_new_purchase(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        worker.last_heartbeat = time.time() - 60
        try:
            await worker.may_submit_buy("run-stale")
        except Exception as exc:
            assert "heartbeat" in str(exc).lower()
        else:
            raise AssertionError("stale worker heartbeat must block new purchases")
        await worker.close()

    asyncio.run(scenario())


def test_stop_requested_after_proposal_blocks_purchase(monkeypatch):
    async def scenario():
        worker = make_worker(monkeypatch)
        events = []

        async def rows(table, _params):
            if table == "bot_runs":
                return [{"status": "stopping", "stop_reason": "User requested stop."}]
            return []

        async def set_run(run_id, status, **fields):
            events.append((run_id, status, fields.get("stop_reason")))

        worker.rows = rows
        worker.set_run = set_run
        allowed = await worker.may_submit_buy("run-stop")
        assert allowed is False
        assert events and events[0][1] == "stopped"
        await worker.close()

    asyncio.run(scenario())
