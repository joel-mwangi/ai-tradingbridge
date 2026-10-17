from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_bot_run_control_requires_authenticated_user():
    response = client.get("/bots/runs")
    assert response.status_code == 401


def test_bot_run_control_does_not_accept_unbounded_or_non_digit_config():
    response = client.post(
        "/bots/runs",
        json={
            "account_id": "CR123",
            "config": {
                "id": "bot-1",
                "name": "Test bot",
                "symbol": "1HZ100V",
                "contractType": "CALL",
                "duration": 10,
                "durationUnit": "t",
                "stakeStrategy": {"type": "martingale", "base": 1, "multiplier": 2, "maxStake": 100},
                "stopConditions": [{"type": "tradeCount", "max": 20}],
            },
        },
    )
    assert response.status_code == 401


def test_bot_run_control_requires_fixed_stake_and_tick_duration():
    response = client.post(
        "/bots/runs",
        json={
            "account_id": "CR123",
            "config": {
                "id": "bot-1",
                "name": "Test bot",
                "symbol": "1HZ100V",
                "contractType": "DIGITEVEN",
                "duration": 11,
                "durationUnit": "s",
                "stakeStrategy": {"type": "fixed", "amount": 1},
                "stopConditions": [{"type": "tradeCount", "max": 20}],
            },
        },
    )
    assert response.status_code == 401


def test_bot_run_queue_requires_a_fresh_worker_heartbeat(monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone

    import pytest
    from fastapi import HTTPException
    import app.bot_control as control

    async def fresh(_method, _table, **_kwargs):
        return [{"last_seen_at": datetime.now(timezone.utc).isoformat()}]

    async def stale(_method, _table, **_kwargs):
        return [{"last_seen_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}]

    async def missing(_method, _table, **_kwargs):
        return []

    monkeypatch.setattr(control, "_db_request", fresh)
    asyncio.run(control._require_live_worker())

    monkeypatch.setattr(control, "_db_request", stale)
    with pytest.raises(HTTPException) as stale_error:
        asyncio.run(control._require_live_worker())
    assert stale_error.value.status_code == 503

    monkeypatch.setattr(control, "_db_request", missing)
    with pytest.raises(HTTPException) as missing_error:
        asyncio.run(control._require_live_worker())
    assert missing_error.value.status_code == 503



def test_account_wide_execution_guard_blocks_another_bot(monkeypatch):
    import asyncio
    import pytest
    from fastapi import HTTPException
    import app.bot_control as control

    async def active_run(_method, _table, **_kwargs):
        return [{"id": "run-active", "status": "running", "bot_id": "different-bot"}]

    monkeypatch.setattr(control, "_db_request", active_run)
    with pytest.raises(HTTPException) as blocked:
        asyncio.run(control._require_account_execution_clear("user-1", "VRTC123"))
    assert blocked.value.status_code == 409
    assert "Another bot run" in blocked.value.detail


def test_reconciliation_lock_blocks_every_bot_for_account(monkeypatch):
    import asyncio
    import pytest
    from fastapi import HTTPException
    import app.bot_control as control

    async def unresolved_run(_method, _table, **_kwargs):
        return [{"id": "run-unknown", "status": "reconciliation_required", "bot_id": "old-bot"}]

    monkeypatch.setattr(control, "_db_request", unresolved_run)
    with pytest.raises(HTTPException) as blocked:
        asyncio.run(control._require_account_execution_clear("user-1", "VRTC123"))
    assert blocked.value.status_code == 409
    assert "unresolved contract" in blocked.value.detail
