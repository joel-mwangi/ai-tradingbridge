import argparse
import json

import pytest

from scripts import even_odd_research


@pytest.mark.asyncio
async def test_research_cli_blocks_duplicate_samples_without_explicit_opt_in(monkeypatch, capsys):
    row = {
        "id": 1,
        "collection_id": "run-1",
        "sequence_no": 1,
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

    args = argparse.Namespace(symbol="R_50", limit=5000, allow_duplicates=False)
    result = await even_odd_research._run(args)
    output = json.loads(capsys.readouterr().out)

    assert result == 2
    assert output["status"] == "blocked"
    assert output["research_run_id"] == "research-run-id"
    assert output["quality"]["exact_market_observation_duplicates"] == 1
    assert output["model_training_performed"] is False
    assert failed == ["research-run-id"]


@pytest.mark.asyncio
async def test_research_cli_blocks_when_no_live_observations_exist(monkeypatch, capsys):
    load_kwargs = {}

    async def fake_load(**kwargs):
        load_kwargs.update(kwargs)
        return {"rows": [], "snapshot_max_id": None}

    async def unexpected_start(**kwargs):
        raise AssertionError("A research run must not start without live observations.")

    monkeypatch.setattr(even_odd_research, "load_historical_ticks", fake_load)
    monkeypatch.setattr(even_odd_research, "start_research_run", unexpected_start)

    args = argparse.Namespace(symbol="R_50", limit=5000, allow_duplicates=False)
    result = await even_odd_research._run(args)
    output = json.loads(capsys.readouterr().out)

    assert result == 2
    assert load_kwargs["capture_mode"] == "all_live"
    assert output["status"] == "blocked"
    assert "No live-captured observations" in output["reason"]
    assert output["historical_imports_enabled"] is False
    assert output["model_training_performed"] is False


@pytest.mark.asyncio
async def test_research_cli_persists_pattern_report_without_training_classifier(monkeypatch, capsys):
    rows = [
        {
            "id": index + 1,
            "collection_id": "run-1",
            "sequence_no": index + 1,
            "market_symbol": "R_50",
            "tick_epoch": 1760000000 + index,
            "quote": f"123.45{index % 10}",
            "pip_size": 3,
            "last_digit": index % 10,
            "parity": "even" if index % 10 % 2 == 0 else "odd",
        }
        for index in range(120)
    ]
    saved = {}

    async def fake_load(**kwargs):
        return {"rows": rows, "snapshot_max_id": 120}

    async def fake_start(**kwargs):
        return "research-run-id"

    async def fake_complete(**kwargs):
        saved.update(kwargs)

    monkeypatch.setattr(even_odd_research, "load_historical_ticks", fake_load)
    monkeypatch.setattr(even_odd_research, "start_research_run", fake_start)
    monkeypatch.setattr(even_odd_research, "complete_research_run", fake_complete)
    monkeypatch.setattr(even_odd_research, "discover_patterns", lambda digits: {"status": "exploratory", "observations": len(digits)})

    args = argparse.Namespace(symbol="R_50", limit=5000, allow_duplicates=False)
    result = await even_odd_research._run(args)
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert saved["model_id"] is None
    assert saved["partition_sizes"] == {"pattern_discovery_observations": 120}
    assert saved["evaluation"]["pattern_discovery"]["status"] == "exploratory"
    assert saved["evaluation"]["model_training"]["performed"] is False
    assert output["model_training_performed"] is False
    assert output["model_id"] is None
