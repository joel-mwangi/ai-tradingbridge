from __future__ import annotations

import pytest

from app.bot_worker import mark_startup_recovery


class FakeStorage:
    def __init__(self):
        self.runs = [
            {"id": "safe-run", "status": "running"},
            {"id": "uncertain-run", "status": "running"},
            {"id": "stopping-run", "status": "stopping"},
        ]
        self.trades = {
            "safe-run": [],
            "uncertain-run": [{"id": "trade-1", "state": "buy_pending", "contract_id": None}],
            "stopping-run": [{"id": "trade-2", "state": "settling", "contract_id": "123"}],
        }
        self.run_patches = []
        self.trade_patches = []

    async def request(self, method, table, *, params=None, payload=None, prefer=None):
        if table == "bot_runs":
            return self.runs
        if table == "bot_trades":
            run_id = params["run_id"].removeprefix("eq.")
            return self.trades[run_id]
        raise AssertionError(table)

    async def patch_run(self, run_id, patch, expected_status=None):
        self.run_patches.append((run_id, patch, expected_status))
        return True

    async def patch_trade(self, trade_id, patch):
        self.trade_patches.append((trade_id, patch))


@pytest.mark.asyncio
async def test_restart_marks_safe_run_interrupted_and_never_replays_orders():
    storage = FakeStorage()
    await mark_startup_recovery(storage)

    patches = {run_id: patch for run_id, patch, _ in storage.run_patches}
    assert patches["safe-run"]["status"] == "interrupted"
    assert patches["uncertain-run"]["status"] == "reconciliation_required"
    assert patches["stopping-run"]["status"] == "reconciliation_required"
    assert patches["uncertain-run"]["ended_at"] is None
    assert storage.trade_patches == [
        ("trade-1", {
            "state": "outcome_unknown",
            "error_code": "WORKER_RESTART_RECONCILIATION_REQUIRED",
            "error_message": "Worker restarted after a possible purchase; automatic retry is prohibited.",
        })
    ]


@pytest.mark.asyncio
async def test_restart_recovery_uses_compare_and_set_expected_status():
    storage = FakeStorage()
    await mark_startup_recovery(storage)
    expected = {run_id: status for run_id, _, status in storage.run_patches}
    assert expected == {
        "safe-run": "running",
        "uncertain-run": "running",
        "stopping-run": "stopping",
    }
