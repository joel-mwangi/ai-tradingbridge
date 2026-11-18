import pytest

from app.even_odd.ml.model import (
    calibrate_model_probabilities,
    predict_even_probability,
    train_logistic_model,
)


def make_rows(start, end):
    rows = []
    for index in range(start, end):
        value = (index % 20) / 20
        rows.append({
            "target_index": index,
            "features": {"signal": value, "noise": float(index % 3)},
            "target_parity": "even" if value >= 0.5 else "odd",
        })
    return rows


def test_logistic_model_trains_with_train_only_scaling_and_predicts_probability():
    train = make_rows(0, 200)
    validation = make_rows(200, 260)
    model = train_logistic_model(
        train,
        validation,
        learning_rate=0.2,
        l2=0.001,
        max_epochs=100,
        patience=15,
    )

    probability = predict_even_probability(model, train[-1]["features"])
    assert 0 <= probability <= 1
    assert model["best_epoch"] > 0
    assert model["training_rows"] == 200
    assert model["validation_rows"] == 60
    assert model["calibrated"] is False
    assert model["validated"] is False
    assert model["execution_authorized"] is False
    assert len(model["means"]) == len(model["feature_names"])


def test_logistic_model_rejects_insufficient_or_single_class_data():
    with pytest.raises(ValueError, match="20 training"):
        train_logistic_model(make_rows(0, 10), make_rows(10, 30))
    single_class = [
        {"features": {"x": float(index)}, "target_parity": "even"}
        for index in range(30)
    ]
    with pytest.raises(ValueError, match="both parity classes"):
        train_logistic_model(single_class, make_rows(200, 230))


def test_prediction_rejects_mismatched_feature_schema():
    model = train_logistic_model(make_rows(0, 100), make_rows(100, 140))
    with pytest.raises(ValueError, match="schema"):
        predict_even_probability(model, {"wrong": 1.0})


def test_training_rejects_inconsistent_or_non_finite_features():
    train = make_rows(0, 100)
    validation = make_rows(100, 140)
    validation[0]["features"]["extra"] = 1.0
    with pytest.raises(ValueError, match="same feature keys"):
        train_logistic_model(train, validation)

    train = make_rows(0, 100)
    train[0]["features"]["signal"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        train_logistic_model(train, make_rows(100, 140))



def test_model_calibration_uses_a_separate_period_and_remains_unvalidated():
    model = train_logistic_model(
        make_rows(0, 200),
        make_rows(200, 260),
        learning_rate=0.2,
        max_epochs=100,
    )
    before = predict_even_probability(model, make_rows(260, 261)[0]["features"])
    calibrated = calibrate_model_probabilities(model, make_rows(260, 340))
    after = predict_even_probability(calibrated, make_rows(260, 261)[0]["features"])

    assert 0 <= before <= 1
    assert 0 <= after <= 1
    assert calibrated["calibrated"] is True
    assert calibrated["validated"] is False
    assert calibrated["execution_authorized"] is False
    assert calibrated["calibration"]["method"] == "platt_scaling"
    assert calibrated["calibration"]["calibration_rows"] == 80


def test_model_calibration_rejects_small_or_single_class_period():
    model = train_logistic_model(make_rows(0, 100), make_rows(100, 140))
    with pytest.raises(ValueError, match="50 calibration"):
        calibrate_model_probabilities(model, make_rows(140, 160))
    single_class = [
        {"features": {"signal": float(i), "noise": 1.0}, "target_parity": "even"}
        for i in range(60)
    ]
    with pytest.raises(ValueError, match="both parity classes"):
        calibrate_model_probabilities(model, single_class)
