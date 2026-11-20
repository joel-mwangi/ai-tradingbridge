import pytest

from app.even_odd.data_quality import (
    canonicalize_overlapping_collection_ticks,
    inspect_historical_ticks,
)


def make_row(*, row_id=1, epoch=1760000000, digit=2, quote="123.452", parity="even", collection="run-a"):
    return {
        "id": row_id,
        "collection_id": collection,
        "sequence_no": row_id,
        "market_symbol": "R_50",
        "tick_epoch": epoch,
        "quote": quote,
        "pip_size": 3,
        "last_digit": digit,
        "parity": parity,
    }


def test_quality_audit_passes_valid_chronological_rows():
    rows = [
        make_row(row_id=1, epoch=1760000000, digit=2, quote="123.452"),
        make_row(row_id=2, epoch=1760000001, digit=3, quote="123.453"),
    ]
    rows[1]["parity"] = "odd"

    result = inspect_historical_ticks(rows, expected_symbol="R_50")
    assert result["quality_passed"] is True
    assert result["valid_rows"] == 2
    assert result["invalid_rows"] == 0
    assert result["first_tick_epoch"] == 1760000000
    assert result["last_tick_epoch"] == 1760000001


def test_quality_audit_detects_parity_quote_and_symbol_mismatches():
    row = make_row(digit=3, quote="123.452", parity="even")
    row["market_symbol"] = "R_100"
    result = inspect_historical_ticks([row], expected_symbol="R_50")

    assert result["quality_passed"] is False
    assert result["symbol_mismatches"] == 1
    assert result["parity_mismatches"] == 1
    assert result["quote_digit_mismatches"] == 1


def test_quality_audit_reports_duplicate_observations_without_dropping_rows():
    row = make_row()
    result = inspect_historical_ticks([row, dict(row)])

    assert result["sample_size"] == 2
    assert result["valid_rows"] == 2
    assert result["exact_market_observation_duplicates"] == 1
    assert result["quality_passed"] is False
    assert "Raw historical records are retained" in result["duplicate_policy"]


def test_quality_audit_detects_non_chronological_order():
    rows = [
        make_row(row_id=2, epoch=1760000001, digit=3, quote="123.453", parity="odd"),
        make_row(row_id=1, epoch=1760000000, digit=2, quote="123.452"),
    ]
    result = inspect_historical_ticks(rows)
    assert result["chronological_order_violations"] == 1
    assert result["quality_passed"] is False


@pytest.mark.parametrize("bad_value", [True, 1.5, "1"])
def test_quality_audit_rejects_non_integer_index_fields(bad_value):
    row = make_row()
    row["last_digit"] = bad_value
    result = inspect_historical_ticks([row])
    assert result["invalid_rows"] == 1
    assert result["quality_passed"] is False


def test_quality_audit_reports_missing_fields():
    result = inspect_historical_ticks([{"id": 1}])
    assert result["missing_field_rows"] == 1
    assert result["invalid_rows"] == 1
    assert result["quality_passed"] is False


def test_quality_audit_rejects_non_collection_input():
    with pytest.raises(ValueError, match="list or tuple"):
        inspect_historical_ticks({"id": 1})



def test_quality_audit_detects_duplicate_and_out_of_order_collection_sequences():
    rows = [
        make_row(row_id=1, epoch=1760000000, digit=2, quote="123.452"),
        make_row(row_id=2, epoch=1760000001, digit=3, quote="123.453", parity="odd"),
    ]
    rows[1]["sequence_no"] = 1
    result = inspect_historical_ticks(rows)
    assert result["duplicate_sequence_numbers"] == 1
    assert result["sequence_order_violations"] == 1
    assert result["quality_passed"] is False



def test_canonical_view_excludes_only_cross_collection_overlap_and_preserves_raw_rows():
    first = make_row(row_id=1, collection="run-a")
    overlap = make_row(row_id=2, collection="run-b")
    same_collection_repeat = make_row(
        row_id=3, collection="run-a", epoch=1760000000,
        digit=2, quote="123.452"
    )
    different_tick = make_row(
        row_id=4, collection="run-b", epoch=1760000001,
        digit=3, quote="123.453", parity="odd"
    )

    result = canonicalize_overlapping_collection_ticks(
        [first, overlap, same_collection_repeat, different_tick]
    )

    assert result["raw_rows"] == 4
    assert result["canonical_rows"] == 3
    assert result["overlap_duplicates_excluded"] == 1
    assert [row["id"] for row in result["rows"]] == [1, 3, 4]
    assert len([first, overlap, same_collection_repeat, different_tick]) == 4


def test_canonical_view_keeps_identical_fingerprints_within_same_collection():
    rows = [
        make_row(row_id=1, collection="run-a"),
        make_row(row_id=2, collection="run-a"),
    ]
    result = canonicalize_overlapping_collection_ticks(rows)
    assert result["canonical_rows"] == 2
    assert result["overlap_duplicates_excluded"] == 0


def test_live_quality_reports_same_epoch_quote_repeats_without_blocking_or_dropping():
    first = make_row(row_id=1, collection="live-batch-1")
    second = make_row(row_id=2, collection="live-batch-2")
    first["capture_mode"] = "live_stream"
    second["capture_mode"] = "live_stream"

    result = inspect_historical_ticks([first, second], expected_symbol="R_50")
    canonical = canonicalize_overlapping_collection_ticks([first, second])

    assert result["quality_passed"] is True
    assert result["exact_market_observation_duplicates"] == 0
    assert result["potential_same_epoch_quote_repeats"] == 1
    assert canonical["canonical_rows"] == 2
    assert canonical["overlap_duplicates_excluded"] == 0
    assert [row["id"] for row in canonical["rows"]] == [1, 2]
