import pytest

from app.even_odd.ml.features import (
    build_next_tick_features,
    build_supervised_rows,
    chronological_split,
)


def test_feature_rows_use_only_ticks_before_target():
    digits = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1]
    rows = build_supervised_rows(digits, windows=(4,))
    assert len(rows) == 8
    first = rows[0]
    assert first["target_index"] == 4
    assert first["target_parity"] == "even"
    assert first["features"]["w4_even_rate"] == 0.5
    assert "target_parity" not in first["features"]
    assert "target_digit" not in first["features"]


def test_feature_rows_are_chronological_and_have_expected_dimensions():
    digits = [i % 10 for i in range(120)]
    rows = build_supervised_rows(digits, windows=(20, 50))
    assert [row["target_index"] for row in rows] == list(range(50, 120))
    assert all(row["target_parity"] in {"even", "odd"} for row in rows)
    assert len(rows[0]["features"]) == len(rows[-1]["features"])


def test_features_change_when_only_past_history_changes():
    first = [0, 2, 4, 6, 8, 1, 3, 5, 7, 9, 2]
    second = [1, 3, 5, 7, 9, 0, 2, 4, 6, 8, 2]
    row_a = build_supervised_rows(first, windows=(10,))[0]
    row_b = build_supervised_rows(second, windows=(10,))[0]
    assert row_a["target_parity"] == row_b["target_parity"]
    assert row_a["features"] != row_b["features"]


def test_no_rows_when_history_is_not_long_enough():
    assert build_supervised_rows([1, 2, 3, 4], windows=(4,)) == []


@pytest.mark.parametrize(
    ("digits", "windows"),
    [
        ([True, 1, 2], (2,)),
        ([0, 1, 10], (2,)),
        ([0, 1, 2], (1,)),
        ([0, 1, 2], (2, 2)),
        ([0, 1, 2], (2, 0)),
    ],
)
def test_feature_builder_rejects_invalid_inputs(digits, windows):
    with pytest.raises(ValueError):
        build_supervised_rows(digits, windows=windows)


def test_target_digit_is_not_encoded_in_features():
    digits = [0, 2, 4, 6, 8, 7]
    rows = build_supervised_rows(digits, windows=(5,))
    assert rows[0]["target_parity"] == "odd"
    assert all(not key.endswith("_target_digit") for key in rows[0]["features"])



def test_chronological_split_preserves_order_and_untouched_test_tail():
    rows = [{"target_index": i, "features": {}, "target_parity": "even"} for i in range(100)]
    splits = chronological_split(rows)
    assert len(splits["train"]) == 60
    assert len(splits["validation"]) == 20
    assert len(splits["test"]) == 20
    assert splits["train"][-1]["target_index"] < splits["validation"][0]["target_index"]
    assert splits["validation"][-1]["target_index"] < splits["test"][0]["target_index"]


def test_chronological_split_rejects_shuffled_or_overlapping_rows():
    rows = [{"target_index": i, "features": {}, "target_parity": "even"} for i in range(10)]
    rows[3], rows[4] = rows[4], rows[3]
    with pytest.raises(ValueError, match="ordered"):
        chronological_split(rows)


def test_chronological_split_requires_untouched_test_fraction():
    rows = [{"target_index": i} for i in range(10)]
    with pytest.raises(ValueError, match="leave a test set"):
        chronological_split(rows, train_fraction=0.8, validation_fraction=0.2)



def test_next_tick_features_do_not_depend_on_placeholder_target_value():
    digits = [0, 2, 4, 6, 8, 1, 3, 5, 7, 9]
    features = build_next_tick_features(digits, windows=(10,))
    with_zero = build_supervised_rows([*digits, 0], windows=(10,))[-1]["features"]
    with_nine = build_supervised_rows([*digits, 9], windows=(10,))[-1]["features"]
    assert features == with_zero == with_nine


def test_next_tick_features_require_complete_history():
    with pytest.raises(ValueError, match="Not enough historical digits"):
        build_next_tick_features([0, 1, 2], windows=(4,))
