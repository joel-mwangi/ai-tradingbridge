import csv

import pytest

from app import collector


@pytest.fixture
def output_csv(tmp_path, monkeypatch):
    path = tmp_path / "ticks.csv"
    monkeypatch.setattr(collector, "CSV_PATH", path)
    monkeypatch.setattr(collector, "DATA_DIR", tmp_path)
    collector.prepare_csv()
    return path


def tick(**overrides):
    value = {
        "symbol": collector.SYMBOL,
        "quote": "123.45",
        "epoch": 1_800_000_000,
        "pip_size": 2,
        "id": "tick-1",
    }
    value.update(overrides)
    return value


def test_saves_valid_tick(output_csv):
    assert collector.save_tick(tick()) is True
    with output_csv.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 1
    assert rows[0]["last_digit"] == "5"
    assert rows[0]["symbol"] == collector.SYMBOL


@pytest.mark.parametrize("bad_tick", [
    None,
    {},
    tick(quote="nan"),
    tick(quote="inf"),
    tick(quote="-inf"),
    tick(quote="0"),
    tick(epoch=1.5),
    tick(epoch=0),
    tick(pip_size=2.5),
    tick(pip_size=True),
    tick(pip_size=11),
    tick(symbol="OTHER"),
])
def test_rejects_invalid_tick_without_writing(output_csv, bad_tick):
    assert collector.save_tick(bad_tick) is False
    with output_csv.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert rows == []
