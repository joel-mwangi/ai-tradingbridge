from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_bot_run_control_requires_authenticated_user():
    response = client.get("/bots/runs")
    assert response.status_code == 401


def test_bot_run_control_does_not_accept_unbounded_or_non_digit_config():
    response = client.post(
        "/bots/runs",
        json={
            "account_id": "CR123",
            "config": {
                "id": "bot-1",
                "name": "Test bot",
                "symbol": "1HZ100V",
                "contractType": "CALL",
                "duration": 10,
                "durationUnit": "t",
                "stakeStrategy": {"type": "martingale", "base": 1, "multiplier": 2, "maxStake": 100},
                "stopConditions": [{"type": "tradeCount", "max": 20}],
            },
        },
    )
    assert response.status_code == 401


def test_bot_run_control_requires_fixed_stake_and_tick_duration():
    response = client.post(
        "/bots/runs",
        json={
            "account_id": "CR123",
            "config": {
                "id": "bot-1",
                "name": "Test bot",
                "symbol": "1HZ100V",
                "contractType": "DIGITEVEN",
                "duration": 11,
                "durationUnit": "s",
                "stakeStrategy": {"type": "fixed", "amount": 1},
                "stopConditions": [{"type": "tradeCount", "max": 20}],
            },
        },
    )
    assert response.status_code == 422
