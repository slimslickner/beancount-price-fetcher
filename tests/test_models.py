"""Tests for the dataclasses and Frequency enum in models.py.

These are mostly trivial — the dataclasses do their own validation via
slots/frozen. We just confirm they construct with the expected attributes
and that Frequency is a proper Enum.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from beancount_price_fetcher.models import (
    CommodityMetadata,
    FetchedPrice,
    Frequency,
    HeldPeriod,
    PriceRequirement,
)


def test_frequency_is_enum() -> None:
    assert Frequency.DAILY.value == "daily"
    assert Frequency.WEEKLY_FRIDAY.value == "weekly-friday"
    assert Frequency.MONTHLY_LAST.value == "monthly-last"


def test_held_period_frozen() -> None:
    hr = HeldPeriod(first=date(2020, 1, 1), last=date(2024, 12, 31), is_open=True)
    assert hr.first == date(2020, 1, 1)
    assert hr.last == date(2024, 12, 31)
    assert hr.is_open is True


def test_commodity_metadata_frozen() -> None:
    cm = CommodityMetadata(
        commodity="SPY",
        ticker="SPY",
        quote_currency="USD",
        frequency=Frequency.DAILY,
        price_start_date=None,
    )
    assert cm.commodity == "SPY"
    assert cm.ticker == "SPY"
    assert cm.price_start_date is None


def test_price_requirement_frozen() -> None:
    pr = PriceRequirement(
        commodity="SPY",
        ticker="SPY",
        quote_currency="USD",
        frequency=Frequency.DAILY,
        min_date=date(2020, 1, 1),
        max_date=date(2024, 12, 31),
        missing_dates=frozenset({date(2020, 1, 2), date(2020, 1, 3)}),
    )
    assert pr.commodity == "SPY"
    assert len(pr.missing_dates) == 2


def test_fetched_price_frozen() -> None:
    fp = FetchedPrice(
        commodity="SPY",
        quote_currency="USD",
        date=date(2020, 1, 2),
        price=Decimal("300.00"),
    )
    assert fp.price == Decimal("300.00")
    assert isinstance(fp.price, Decimal)


def test_dataclass_immutability() -> None:
    """frozen=True should prevent mutation."""
    fp = FetchedPrice(
        commodity="SPY",
        quote_currency="USD",
        date=date(2020, 1, 2),
        price=Decimal("300.00"),
    )
    try:
        fp.price = Decimal("999.99")  # type: ignore[misc]
    except Exception as exc:
        msg = str(exc).lower()
        assert "frozen" in msg or "attribute" in msg or "cannot assign" in msg
    else:
        raise AssertionError("Expected frozen dataclass to reject assignment")
