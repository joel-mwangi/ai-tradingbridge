import math

from app.contract_normalization import (
    is_deriv_true,
    is_terminal_contract,
    normalize_settlement,
    parse_deriv_number,
)


def test_number_parser_preserves_zero_and_rejects_invalid_values():
    assert parse_deriv_number(0) == 0.0
    assert parse_deriv_number("0") == 0.0
    assert parse_deriv_number("46.13") == 46.13
    assert parse_deriv_number(True) is None
    assert parse_deriv_number("nan") is None
    assert parse_deriv_number("not-money") is None
    assert parse_deriv_number(None) is None


def test_terminal_flags_parse_false_string_safely():
    assert is_deriv_true(True)
    assert is_deriv_true("true")
    assert not is_deriv_true("false")
    assert not is_deriv_true("0")
    assert is_terminal_contract({"status": "won"})
    assert is_terminal_contract({"is_sold": "1"})
    assert not is_terminal_contract({"status": "open", "is_sold": "false"})


def test_winning_contract_uses_confirmed_profit_and_payout():
    result = normalize_settlement(
        {"status": "won", "payout": "46.13", "profit": "21.13"},
        stake=25.0,
    )
    assert result == {
        "provider_status": "won",
        "payout": 46.13,
        "profit": 21.13,
        "outcome": "won",
    }


def test_losing_contract_does_not_fall_back_to_potential_payout():
    result = normalize_settlement(
        {"status": "lost", "sell_price": "0", "payout": "46.13"},
        stake=25.0,
    )
    assert result == {
        "provider_status": "lost",
        "payout": 0.0,
        "profit": -25.0,
        "outcome": "lost",
    }


def test_early_sale_preserves_explicit_zero_sell_price():
    result = normalize_settlement(
        {"status": "sold", "is_sold": True, "sell_price": "0", "payout": "46.13"},
        stake=25.0,
    )
    assert result == {
        "provider_status": "sold",
        "payout": 0.0,
        "profit": -25.0,
        "outcome": "lost",
    }


def test_explicit_profit_is_preferred_when_payout_is_missing():
    result = normalize_settlement({"status": "won", "profit": "21.13"}, stake=25.0)
    assert result == {
        "provider_status": "won",
        "payout": 46.13,
        "profit": 21.13,
        "outcome": "won",
    }


def test_nonterminal_or_unreconciled_contract_is_not_settled():
    assert normalize_settlement({"status": "open", "payout": 46.13}, stake=25.0) is None
    assert normalize_settlement({"status": "expired"}, stake=25.0) is None
