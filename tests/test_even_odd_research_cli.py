import argparse
import json

import pytest

from scripts import even_odd_research


@pytest.mark.asyncio
async def test_research_cli_blocks_duplicate_samples_without_explicit_opt_in(monkeypatch, capsys):
    row = {
        "id": 1,
        "collection_id": "run-1",
        "market_symbol": "R_50",
        "tick_epoch": 1760000000,
        "quote": "123.452",
        "pip_size": 3,
        "last_digit": 2,
        "parity": "even",
    }
    failed = []

    async def fake_load(**kwargs):
        return {"rows": [row, dict(row)], "snapshot_max_id": 2}

    async def fake_start(**kwargs):
        return "research-run-id"

    async def fake_fail(**kwargs):
        failed.append(kwargs["research_run_id"])

    monkeypatch.setattr(even_odd_research, "load_historical_ticks", fake_load)
    monkeypatch.setattr(even_odd_research, "start_research_run", fake_start)
    monkeypatch.setattr(even_odd_research, "fail_research_run", fake_fail)

    args = argparse.Namespace(
        symbol="R_50",
        limit=5000,
        windows=[2],
        max_epochs=10,
        walk_forward=False,
        allow_duplicates=False,
        minimum_train_rows=100,
        test_rows=50,
        maximum_folds=2,
    )
    result = await even_odd_research._run(args)
    output = json.loads(capsys.readouterr().out)

    assert result == 2
    assert output["status"] == "blocked"
    assert output["research_run_id"] == "research-run-id"
    assert output["quality"]["exact_market_observation_duplicates"] == 1
    assert failed == ["research-run-id"]
