"""Tests for beancount_price_fetcher.requirements.

These don't need the beancount fixture - we hand-build small HeldPeriod
sets and verify the required-date generation logic.
"""

from __future__ import annotations

from datetime import date

from beancount_price_fetcher.models import (
    CommodityMetadata,
    Frequency,
    HeldPeriod,
)
from beancount_price_fetcher.requirements import (
    compute_requirements,
    date_ranges_from_dates,
    required_dates_in_range,
)


def test_required_dates_daily_returns_all_weekdays() -> None:
    """Mon-Fri range yields every weekday."""
    dates = required_dates_in_range(date(2020, 1, 6), date(2020, 1, 10), Frequency.DAILY)
    assert dates == frozenset(
        {date(2020, 1, 6), date(2020, 1, 7), date(2020, 1, 8), date(2020, 1, 9), date(2020, 1, 10)}
    )


def test_required_dates_daily_skips_weekends() -> None:
    """Sat-Sun range yields no dates."""
    dates = required_dates_in_range(date(2020, 1, 11), date(2020, 1, 12), Frequency.DAILY)
    assert dates == frozenset()


def test_required_dates_weekly_friday_returns_only_fridays() -> None:
    """Weekly-Friday: every Friday in the range."""
    dates = required_dates_in_range(date(2020, 1, 6), date(2020, 1, 19), Frequency.WEEKLY_FRIDAY)
    # Range 2020-01-06 (Mon) to 2020-01-19 (Sun) contains two Fridays.
    assert dates == frozenset({date(2020, 1, 10), date(2020, 1, 17)})


def test_required_dates_monthly_last_returns_last_business_day() -> None:
    """Monthly-last: last weekday of each calendar month in range."""
    dates = required_dates_in_range(date(2020, 1, 1), date(2020, 2, 29), Frequency.MONTHLY_LAST)
    assert dates == frozenset({date(2020, 1, 31), date(2020, 2, 28)})


def test_required_dates_single_day_weekday() -> None:
    """Single weekday in range yields itself."""
    dates = required_dates_in_range(date(2020, 1, 8), date(2020, 1, 8), Frequency.DAILY)
    assert dates == frozenset({date(2020, 1, 8)})


def test_required_dates_empty_when_end_before_start() -> None:
    """End before start yields empty set."""
    dates = required_dates_in_range(date(2020, 1, 10), date(2020, 1, 6), Frequency.DAILY)
    assert dates == frozenset()


def test_compute_requirements_simple_case() -> None:
    """One commodity, daily frequency, one missing date."""
    held_periods: dict[str, list[HeldPeriod]] = {
        "SPY": [HeldPeriod(date(2020, 1, 6), date(2020, 1, 10), is_open=False)]
    }
    existing: dict[str, set[date]] = {"SPY": {date(2020, 1, 6)}}
    metadata: dict[str, CommodityMetadata] = {
        "SPY": CommodityMetadata("SPY", "SPY", "USD", None, None)
    }
    reqs = compute_requirements(held_periods, existing, metadata, Frequency.DAILY)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.commodity == "SPY"
    assert r.frequency == Frequency.DAILY
    assert r.min_date == date(2020, 1, 6)
    assert r.max_date == date(2020, 1, 10)
    assert r.missing_dates == frozenset(
        {date(2020, 1, 7), date(2020, 1, 8), date(2020, 1, 9), date(2020, 1, 10)}
    )


def test_compute_requirements_multi_period_fills_both_windows() -> None:
    """AAPL-style: two held periods -> single PriceRequirement covering both.

    Required dates are the UNION of dates required in each period, minus
    existing. The PriceRequirement's min/max spans the overall range so a
    single yfinance ``history()`` call covers both.
    """
    held_periods: dict[str, list[HeldPeriod]] = {
        "AAPL": [
            HeldPeriod(date(2021, 1, 15), date(2022, 6, 30), is_open=False),
            HeldPeriod(date(2023, 3, 1), date(2024, 12, 31), is_open=True),
        ]
    }
    existing: dict[str, set[date]] = {"AAPL": {date(2021, 1, 15), date(2022, 6, 30)}}
    metadata: dict[str, CommodityMetadata] = {
        "AAPL": CommodityMetadata("AAPL", "AAPL", "USD", None, None)
    }
    reqs = compute_requirements(held_periods, existing, metadata, Frequency.DAILY)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.min_date == date(2021, 1, 15)
    assert r.max_date == date(2024, 12, 31)
    assert date(2021, 1, 15) not in r.missing_dates
    assert date(2022, 6, 30) not in r.missing_dates
    assert date(2021, 1, 19) in r.missing_dates
    assert date(2023, 3, 2) in r.missing_dates
    assert date(2022, 7, 1) not in r.missing_dates
    assert date(2023, 2, 28) not in r.missing_dates


def test_compute_requirements_respects_frequency_override() -> None:
    held_periods: dict[str, list[HeldPeriod]] = {
        "FXAIX": [HeldPeriod(date(2020, 1, 1), date(2020, 3, 31), is_open=False)]
    }
    existing: dict[str, set[date]] = {}
    metadata: dict[str, CommodityMetadata] = {
        "FXAIX": CommodityMetadata("FXAIX", "FXAIX", "USD", Frequency.MONTHLY_LAST, None)
    }
    reqs = compute_requirements(held_periods, existing, metadata, Frequency.DAILY)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.frequency == Frequency.MONTHLY_LAST
    assert r.missing_dates == frozenset({date(2020, 1, 31), date(2020, 2, 28), date(2020, 3, 31)})


def test_compute_requirements_respects_start_date_override() -> None:
    """price-start-date override pushes min_date earlier than HeldPeriod.first."""
    held_periods: dict[str, list[HeldPeriod]] = {
        "GOOG": [HeldPeriod(date(2019, 1, 2), date(2020, 12, 31), is_open=False)]
    }
    existing: dict[str, set[date]] = {"GOOG": {date(2018, 1, 1), date(2019, 1, 2)}}
    metadata: dict[str, CommodityMetadata] = {
        "GOOG": CommodityMetadata("GOOG", "GOOG", "USD", None, date(2018, 1, 1))
    }
    reqs = compute_requirements(held_periods, existing, metadata, Frequency.DAILY)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.min_date == date(2018, 1, 1)
    assert r.max_date == date(2020, 12, 31)


def test_compute_requirements_no_metadata_uses_fallback_ticker() -> None:
    """Commodity without Commodity directive: ticker = commodity code."""
    held_periods: dict[str, list[HeldPeriod]] = {
        "MSFT": [HeldPeriod(date(2021, 6, 1), date(2024, 12, 31), is_open=True)]
    }
    existing: dict[str, set[date]] = {}
    metadata: dict[str, CommodityMetadata] = {}
    reqs = compute_requirements(held_periods, existing, metadata, Frequency.DAILY)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.ticker == "MSFT"
    assert r.quote_currency == "USD"


def test_compute_requirements_skips_fully_covered_commodity() -> None:
    """If every required date is already present, no requirement is emitted."""
    held_periods: dict[str, list[HeldPeriod]] = {
        "SPY": [HeldPeriod(date(2020, 1, 6), date(2020, 1, 10), is_open=False)]
    }
    existing: dict[str, set[date]] = {
        "SPY": {
            date(2020, 1, 6),
            date(2020, 1, 7),
            date(2020, 1, 8),
            date(2020, 1, 9),
            date(2020, 1, 10),
        }
    }
    metadata: dict[str, CommodityMetadata] = {
        "SPY": CommodityMetadata("SPY", "SPY", "USD", None, None)
    }
    reqs = compute_requirements(held_periods, existing, metadata, Frequency.DAILY)
    assert reqs == []


def test_compute_requirements_handles_multiple_commodities() -> None:
    held_periods: dict[str, list[HeldPeriod]] = {
        "SPY": [HeldPeriod(date(2020, 1, 6), date(2020, 1, 10), is_open=False)],
        "AAPL": [HeldPeriod(date(2020, 1, 6), date(2020, 1, 10), is_open=False)],
    }
    existing: dict[str, set[date]] = {}
    metadata = {
        "SPY": CommodityMetadata("SPY", "SPY", "USD", None, None),
        "AAPL": CommodityMetadata("AAPL", "AAPL", "USD", None, None),
    }
    reqs = compute_requirements(held_periods, existing, metadata, Frequency.DAILY)
    commodities = {r.commodity for r in reqs}
    assert commodities == {"SPY", "AAPL"}


def test_compute_requirements_start_date_after_first_held() -> None:
    """If start-date override is LATER than HeldPeriod.first, the override wins."""
    held_periods: dict[str, list[HeldPeriod]] = {
        "X": [HeldPeriod(date(2020, 1, 1), date(2020, 12, 31), is_open=False)]
    }
    existing: dict[str, set[date]] = {}
    metadata: dict[str, CommodityMetadata] = {
        "X": CommodityMetadata("X", "X", "USD", None, date(2020, 6, 1))
    }
    reqs = compute_requirements(held_periods, existing, metadata, Frequency.DAILY)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.min_date == date(2020, 6, 1)


def test_compute_requirements_empty_held_periods_list() -> None:
    """Commodity with empty periods list yields no requirement."""
    held_periods: dict[str, list[HeldPeriod]] = {"X": []}
    existing: dict[str, set[date]] = {}
    metadata: dict[str, CommodityMetadata] = {"X": CommodityMetadata("X", "X", "USD", None, None)}
    reqs = compute_requirements(held_periods, existing, metadata, Frequency.DAILY)
    assert reqs == []


def test_date_ranges_contiguous_dates_become_one_range() -> None:
    ranges = date_ranges_from_dates({date(2020, 1, 6), date(2020, 1, 7), date(2020, 1, 8)})
    assert ranges == [(date(2020, 1, 6), date(2020, 1, 8))]


def test_date_ranges_with_gap_split() -> None:
    ranges = date_ranges_from_dates(
        {date(2020, 1, 6), date(2020, 1, 7), date(2020, 1, 13), date(2020, 1, 14)}
    )
    assert ranges == [(date(2020, 1, 6), date(2020, 1, 7)), (date(2020, 1, 13), date(2020, 1, 14))]


def test_date_ranges_empty() -> None:
    assert date_ranges_from_dates(set()) == []


def test_date_ranges_single_date() -> None:
    assert date_ranges_from_dates({date(2020, 1, 6)}) == [(date(2020, 1, 6), date(2020, 1, 6))]
