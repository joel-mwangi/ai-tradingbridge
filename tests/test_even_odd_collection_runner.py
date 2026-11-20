import json

from scripts import even_odd_collect_history


def test_historical_collection_runner_is_retired(capsys):
    result = even_odd_collect_history.main()
    output = json.loads(capsys.readouterr().out)

    assert result == 2
    assert output["status"] == "disabled"
    assert "Historical tick collection is retired" in output["reason"]
    assert output["replacement"] == "python -m scripts.even_odd_live_collector"
