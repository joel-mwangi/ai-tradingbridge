import argparse
import json

import pytest

from scripts import even_odd_collect_history


@pytest.mark.asyncio
async def test_collection_runner_persists_bounded_batch_and_checks_final_snapshot(monkeypatch, capsys):
    rows = []
    saved_runs = []

    async def fake_load(**kwargs):
        return {
            "rows": list(rows),
            "snapshot_max_id": len(rows) or None,
        }

    async def fake_start(**kwargs):
        saved_runs.append({"started": kwargs})
        return "collection-run-id"

    async def fake_collect(**kwargs):
        return [
            {
                "epoch": 1_760_000_000 + index,
                "quote": f"123.45{index % 10}",
                "pip_size": 2,
                "last_digit": index % 10,
                "parity": "even" if index % 2 == 0 else "odd",
                "received_at": "2026-11-19T00:00:00+00:00",
            }
            for index in range(kwargs["tick_count"])
        ]

    async def fake_finish(**kwargs):
        saved_runs.append({"finished": kwargs})
        for index, tick in enumerate(kwargs["ticks"], start=1):
            rows.append({
                "id": len(rows) + 1,
                "market_symbol": kwargs["symbol"],
                "tick_epoch": tick["epoch"],
                "quote": tick["quote"],
                "pip_size": tick["pip_size"],
                "last_digit": tick["last_digit"],
                "parity": tick["parity"],
                "sequence_no": index,
            })

    def fake_quality(history, *, expected_symbol):
        return {
            "quality_passed": True,
            "sample_size": len(history),
            "symbol": expected_symbol,
        }

    monkeypatch.setattr(even_odd_collect_history, "load_historical_ticks", fake_load)
    monkeypatch.setattr(even_odd_collect_history, "start_collection_run", fake_start)
    monkeypatch.setattr(even_odd_collect_history, "collect_ticks", fake_collect)
    monkeypatch.setattr(even_odd_collect_history, "finish_collection_run", fake_finish)
    monkeypatch.setattr(even_odd_collect_history, "inspect_historical_ticks", fake_quality)

    args = argparse.Namespace(
        symbol="R_50",
        target_rows=10,
        batch_size=10,
        timeout_seconds=5,
        max_attempts=1,
    )
    result = await even_odd_collect_history._run(args)
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["status"] == "target_reached"
    assert output["rows_available_after_collection"] == 10
    assert output["new_rows_collected"] == 10
    assert len(saved_runs) == 2
    assert saved_runs[1]["finished"]["status"] == "completed"


@pytest.mark.asyncio
async def test_collection_runner_reports_incomplete_when_attempt_limit_is_reached(monkeypatch, capsys):
    rows = []

    async def fake_load(**kwargs):
        return {"rows": list(rows), "snapshot_max_id": None}

    async def fake_start(**kwargs):
        return "collection-run-id"

    async def fake_collect(**kwargs):
        return [
            {
                "epoch": 1_760_000_000 + index,
                "quote": f"123.45{index % 10}",
                "pip_size": 2,
                "last_digit": index % 10,
                "parity": "even" if index % 2 == 0 else "odd",
                "received_at": "2026-11-19T00:00:00+00:00",
            }
            for index in range(10)
        ]

    async def fake_finish(**kwargs):
        for index, tick in enumerate(kwargs["ticks"], start=1):
            rows.append({
                "id": len(rows) + 1,
                "market_symbol": kwargs["symbol"],
                "tick_epoch": tick["epoch"],
                "quote": tick["quote"],
                "pip_size": tick["pip_size"],
                "last_digit": tick["last_digit"],
                "parity": tick["parity"],
                "sequence_no": index,
            })

    monkeypatch.setattr(even_odd_collect_history, "load_historical_ticks", fake_load)
    monkeypatch.setattr(even_odd_collect_history, "start_collection_run", fake_start)
    monkeypatch.setattr(even_odd_collect_history, "collect_ticks", fake_collect)
    monkeypatch.setattr(even_odd_collect_history, "finish_collection_run", fake_finish)
    monkeypatch.setattr(
        even_odd_collect_history,
        "inspect_historical_ticks",
        lambda history, expected_symbol: {"quality_passed": True},
    )

    args = argparse.Namespace(
        symbol="R_50",
        target_rows=30,
        batch_size=10,
        timeout_seconds=5,
        max_attempts=1,
    )
    result = await even_odd_collect_history._run(args)
    output = json.loads(capsys.readouterr().out)

    assert result == 2
    assert output["status"] == "incomplete"
    assert output["remaining_rows"] == 20
    assert output["max_attempts_reached"] is True
