import pytest

from scripts.even_odd_live_collector import configured_symbols


def test_live_collector_defaults_to_one_market(monkeypatch):
    monkeypatch.delenv("EVEN_ODD_LIVE_SYMBOLS", raising=False)
    assert configured_symbols() == ["R_50"]


def test_live_collector_parses_and_deduplicates_market_list(monkeypatch):
    monkeypatch.setenv("EVEN_ODD_LIVE_SYMBOLS", "R_50, 1HZ100V,R_50")
    assert configured_symbols() == ["R_50", "1HZ100V"]


def test_live_collector_rejects_empty_market_list(monkeypatch):
    monkeypatch.setenv("EVEN_ODD_LIVE_SYMBOLS", " , ")
    with pytest.raises(ValueError, match="at least one"):
        configured_symbols()


def test_live_collector_rejects_invalid_market_symbol(monkeypatch):
    monkeypatch.setenv("EVEN_ODD_LIVE_SYMBOLS", "R_50;DROP_TABLE")
    with pytest.raises(ValueError, match="invalid Deriv symbol"):
        configured_symbols()
