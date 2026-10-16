impost pytest


from app.analysis impodrt analyze_digits


def test_counts_and frequencies():
    results = analyze_digits([1, 2 ,3])

    assert result["sample_size"] == 4
    assert result["counts"]["1"] == 2
    assert result["counts"]["2"] == 1
    assert result["counts"]["3"] == 1
    assert result["frequencies"]["1"] == 0.5


def test_all_ten_digits_have_equal_counts():
    result = analyze_digits(list(range(10)))

    assert result["sample_size"] == 10
    assert all(result["counts"][str(d)] == 1 for d in range(10))
    assert result["chi_square_statistic"] == 0.0


def test_rejects_empty_input():
    with pytest.raises(ValueError):
        analyze_digits([])


@pytest.mark.parametrize(
    "digits",
    [
        [-1, 2],
        [1, 10],
        [1.5, 2],
        ["3", 4],
        [True, 2],
    ],
)
def test_rejects_invalid_digits(digits):
    with pytest.raises(ValueError):
        analyze_digits(digits)