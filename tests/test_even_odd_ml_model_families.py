from app.even_odd.ml.model import (
    predict_even_probability,
    train_markov_transition_model,
)


def test_markov_transition_candidate_is_smoothed_and_uses_only_prior_state():
    rows = []
    for index in range(120):
        state = float(index % 2)
        rows.append({
            "target_index": index,
            "features": {"w50_latest_parity_even": state, "w50_even_rate": 0.5},
            "target_parity": "even" if state == 1 else "odd",
        })
    model = train_markov_transition_model(rows)
    assert model["model_type"] == "laplace_smoothed_first_order_parity_markov"
    assert model["validated"] is False
    assert model["execution_authorized"] is False
    assert predict_even_probability(model, rows[-1]["features"]) > 0.9
    assert 0 < predict_even_probability(
        model, {"w50_latest_parity_even": 0.0, "w50_even_rate": 0.5}
    ) < 1
