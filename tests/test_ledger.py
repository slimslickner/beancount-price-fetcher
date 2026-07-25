"""Tests for beancount_price_fetcher.ledger against tests/fixtures/example.beancount.

Hand-computed expectations are encoded directly in the assertions below;
they are derived from the fixture file's documented held periods and
commodity metadata. Tests freeze "today" at 2024-12-31.

Multi-period coverage: AAPL is bought, fully sold, then rebought -- it
must produce two ``HeldPeriod`` entries. GOOG is bought, fully sold -- one
closed period.
"""

from __future__ import annotations

from datetime import date

import pytest
from beancount.loader import load_file
from freezegun import freeze_time

from beancount_price_fetcher.ledger import (
    LedgerAnalysis,
    analyze_ledger,
    compute_held_periods,
    extract_commodity_metadata,
    extract_existing_prices,
)
from beancount_price_fetcher.models import Frequency, HeldPeriod

FIXTURE = "tests/fixtures/example.beancount"
FROZEN_TODAY = "2024-12-31"


@freeze_time(FROZEN_TODAY)
def test_analyze_ledger_returns_ledger_analysis() -> None:
    result = analyze_ledger(FIXTURE)
    assert isinstance(result, LedgerAnalysis)


@freeze_time(FROZEN_TODAY)
def test_analyze_ledger_operating_currency() -> None:
    result = analyze_ledger(FIXTURE)
    assert result.operating_currencies == frozenset({"USD"})


@freeze_time(FROZEN_TODAY)
def test_extract_commodity_metadata_spy() -> None:
    """SPY has a clean price: directive, no overrides."""
    entries, _, _ = _load_fixture()
    meta = extract_commodity_metadata(entries)
    assert "SPY" in meta
    assert meta["SPY"].ticker == "SPY"
    assert meta["SPY"].quote_currency == "USD"
    assert meta["SPY"].frequency is None
    assert meta["SPY"].price_start_date is None


@freeze_time(FROZEN_TODAY)
def test_extract_commodity_metadata_fxaix_has_frequency_override() -> None:
    entries, _, _ = _load_fixture()
    meta = extract_commodity_metadata(entries)
    assert meta["FXAIX"].frequency == Frequency.MONTHLY_LAST


@freeze_time(FROZEN_TODAY)
def test_extract_commodity_metadata_goog_has_start_date_override() -> None:
    entries, _, _ = _load_fixture()
    meta = extract_commodity_metadata(entries)
    assert meta["GOOG"].price_start_date == date(2018, 1, 1)


@freeze_time(FROZEN_TODAY)
def test_extract_commodity_metadata_eur_has_quote_currency() -> None:
    entries, _, _ = _load_fixture()
    meta = extract_commodity_metadata(entries)
    assert meta["EUR"].ticker == "EURUSD=X"
    assert meta["EUR"].quote_currency == "USD"


@freeze_time(FROZEN_TODAY)
def test_extract_commodity_metadata_no_commodity_directive() -> None:
    """MSFT has no Commodity directive -> fallback applied at requirement time."""
    entries, _, _ = _load_fixture()
    meta = extract_commodity_metadata(entries)
    assert "MSFT" not in meta


# ---- Held period tests (multi-period aware) ----


@freeze_time(FROZEN_TODAY)
def test_held_periods_spy_single_open_period() -> None:
    """SPY: bought 2020-01-02, still held -> one open period spanning to today."""
    periods = compute_held_periods(*_load_fixture_with_today())
    assert periods["SPY"] == [
        HeldPeriod(first=date(2020, 1, 2), last=date(2024, 12, 31), is_open=True)
    ]


@freeze_time(FROZEN_TODAY)
def test_held_periods_aapl_two_periods() -> None:
    """AAPL: bought, sold, rebought -> TWO periods.

    This is the core multi-period case: the user wants the library to
    recognise that AAPL was held in two disjoint windows and fill both.
    """
    periods = compute_held_periods(*_load_fixture_with_today())
    assert periods["AAPL"] == [
        HeldPeriod(first=date(2021, 1, 15), last=date(2022, 6, 30), is_open=False),
        HeldPeriod(first=date(2023, 3, 1), last=date(2024, 12, 31), is_open=True),
    ]


@freeze_time(FROZEN_TODAY)
def test_held_periods_goog_single_closed_period() -> None:
    """GOOG: bought 2019-01-02, sold 2020-12-31 -> one closed period."""
    periods = compute_held_periods(*_load_fixture_with_today())
    assert periods["GOOG"] == [
        HeldPeriod(first=date(2019, 1, 2), last=date(2020, 12, 31), is_open=False)
    ]


@freeze_time(FROZEN_TODAY)
def test_held_periods_eur_non_base_currency() -> None:
    """EUR: non-base currency in Assets/Liab -> held even without cost basis."""
    periods = compute_held_periods(*_load_fixture_with_today())
    assert periods["EUR"] == [
        HeldPeriod(first=date(2021, 3, 15), last=date(2021, 8, 15), is_open=False)
    ]


@freeze_time(FROZEN_TODAY)
def test_held_periods_msft_fallback_path() -> None:
    """MSFT has no Commodity directive but is cost-basis held."""
    periods = compute_held_periods(*_load_fixture_with_today())
    assert periods["MSFT"] == [
        HeldPeriod(first=date(2021, 6, 1), last=date(2024, 12, 31), is_open=True)
    ]


@freeze_time(FROZEN_TODAY)
def test_held_periods_excludes_operating_currency() -> None:
    """USD (operating currency) should never appear in held periods."""
    periods = compute_held_periods(*_load_fixture_with_today())
    assert "USD" not in periods


@freeze_time(FROZEN_TODAY)
def test_held_periods_excludes_income_expense() -> None:
    """EUR flows through Income:Bank / Expenses:Travel don't create held periods."""
    periods = compute_held_periods(*_load_fixture_with_today())
    # EUR is in exactly one period (the bank-account window), not three.
    assert len(periods["EUR"]) == 1


# ---- Existing-price tests ----


@freeze_time(FROZEN_TODAY)
def test_existing_prices_extracted() -> None:
    entries, _, _ = _load_fixture()
    existing = extract_existing_prices(entries)
    assert date(2020, 1, 2) in existing["SPY"]
    assert date(2020, 6, 15) in existing["SPY"]
    assert date(2021, 1, 15) in existing["AAPL"]
    assert date(2022, 6, 30) in existing["AAPL"]
    assert date(2022, 1, 3) in existing["FXAIX"]
    assert date(2022, 1, 31) in existing["FXAIX"]
    assert date(2018, 1, 1) in existing["GOOG"]
    assert date(2019, 1, 2) in existing["GOOG"]
    assert date(2021, 3, 15) in existing["EUR"]
    assert date(2021, 8, 15) in existing["EUR"]
    assert date(2021, 6, 1) in existing["MSFT"]


@freeze_time(FROZEN_TODAY)
def test_existing_prices_only_commodities_with_prices() -> None:
    entries, _, _ = _load_fixture()
    existing = extract_existing_prices(entries)
    assert set(existing.keys()) == {"SPY", "AAPL", "FXAIX", "GOOG", "EUR", "MSFT"}


@freeze_time(FROZEN_TODAY)
def test_analyze_ledger_wires_everything_together() -> None:
    result = analyze_ledger(FIXTURE)
    assert "SPY" in result.metadata
    assert "SPY" in result.held_periods
    assert "SPY" in result.existing_prices
    assert result.today == date(2024, 12, 31)


def test_unfrozen_today_is_today() -> None:
    """If today isn't frozen, analyze_ledger uses today's date."""
    result = analyze_ledger(FIXTURE)
    from datetime import date as _date

    assert isinstance(result.today, _date)


def test_invalid_fixture_raises() -> None:
    with pytest.raises((FileNotFoundError, OSError)):
        analyze_ledger("does/not/exist.beancount")


# ---- helpers ----


def _load_fixture() -> tuple[list, list, dict]:
    """Load the fixture once per test, return (entries, errors, options)."""
    return load_file(FIXTURE)


def _load_fixture_with_today() -> tuple[list, frozenset[str], date]:
    """Return (entries, operating_currencies, today) for held-period tests."""
    entries, _errors, options = load_file(FIXTURE)
    operating_currencies = frozenset(options.get("operating_currency", ["USD"]))
    return entries, operating_currencies, date(2024, 12, 31)
