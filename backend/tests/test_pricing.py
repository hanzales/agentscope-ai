from datetime import date

from app.analytics.pricing import calculate


def test_missing_usage_is_unknown() -> None:
    assert calculate("demo", "simulated-model", None, None, None, date.today()) == (None, None)


def test_unknown_price_is_unknown() -> None:
    assert calculate("unknown", "model", 1, 1, 0, date.today()) == (None, None)
