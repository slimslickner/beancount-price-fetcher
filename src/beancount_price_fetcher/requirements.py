"""Compute which (commodity, date) pairs need prices.

A commodity may have multiple disjoint held periods (e.g. bought, fully
sold, then rebought). We emit one ``PriceRequirement`` per commodity whose
``missing_dates`` is the UNION of dates required in each period, minus
existing prices; ``min_date``/``max_date`` span the full window so a single
yfinance ``history()`` call can cover everything.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable
from datetime import date, timedelta

from .constants import DEFAULT_FREQUENCY
from .models import CommodityMetadata, Frequency, HeldPeriod, PriceRequirement

DEFAULT_QUOTE_CURRENCY = "USD"


def compute_requirements(
    held_periods: dict[str, list[HeldPeriod]],
    existing_prices: dict[str, set[date]],
    metadata: dict[str, CommodityMetadata],
    default_frequency: Frequency = DEFAULT_FREQUENCY,
    default_quote_currency: str = DEFAULT_QUOTE_CURRENCY,
) -> list[PriceRequirement]:
    """Build the list of ``PriceRequirement`` for every commodity needing prices.

    Args:
        held_periods: Per-commodity list of held periods (may be multiple).
        existing_prices: Per-commodity set of dates with existing prices.
        metadata: Per-commodity metadata (ticker, quote currency, overrides).
        default_frequency: Used when a commodity has no ``price-frequency`` override.
        default_quote_currency: Quote currency for the no-metadata fallback
            (commodity code as ticker).

    Returns:
        A list of ``PriceRequirement`` -- one per commodity with non-empty
        missing-date set. Empty list if everything is already priced.
    """
    out: list[PriceRequirement] = []
    for commodity, periods in held_periods.items():
        if not periods:
            continue
        meta = metadata.get(commodity)
        frequency = (meta.frequency if meta else None) or default_frequency
        if meta is not None:
            ticker = meta.ticker
            quote_currency = meta.quote_currency
            price_start = meta.price_start_date
        else:
            ticker = commodity
            quote_currency = default_quote_currency
            price_start = None

        earliest_period_first = min(p.first for p in periods)
        effective_min = price_start if price_start is not None else earliest_period_first
        effective_max = max(p.last for p in periods)

        required: set[date] = set()
        for period in periods:
            period_start = max(period.first, effective_min)
            if period.last < period_start:
                continue
            required |= required_dates_in_range(period_start, period.last, frequency)

        existing = existing_prices.get(commodity, set())
        missing = required - existing
        if not missing:
            continue

        out.append(
            PriceRequirement(
                commodity=commodity,
                ticker=ticker,
                quote_currency=quote_currency,
                frequency=frequency,
                min_date=effective_min,
                max_date=effective_max,
                missing_dates=frozenset(missing),
            )
        )
    return out


def required_dates_in_range(
    start: date,
    end: date,
    frequency: Frequency,
) -> frozenset[date]:
    """Generate the set of dates requiring a price directive in [start, end].

    DAILY: every weekday (Mon-Fri).
    WEEKLY_FRIDAY: every Friday.
    MONTHLY_LAST: last weekday of each calendar month in the range.
    """
    if end < start:
        return frozenset()
    if frequency == Frequency.DAILY:
        return _daily_weekdays(start, end)
    if frequency == Frequency.WEEKLY_FRIDAY:
        return _weekly_fridays(start, end)
    if frequency == Frequency.MONTHLY_LAST:
        return _monthly_last(start, end)
    msg = f"unhandled frequency: {frequency}"
    raise ValueError(msg)


def date_ranges_from_dates(dates: Iterable[date]) -> list[tuple[date, date]]:
    """Collapse a set of dates into a list of contiguous (inclusive) ranges.

    Useful for batched yfinance ``history()`` calls: one call per range
    instead of one per missing date.
    """
    sorted_dates = sorted(set(dates))
    if not sorted_dates:
        return []
    ranges: list[tuple[date, date]] = []
    start = sorted_dates[0]
    prev = start
    for d in sorted_dates[1:]:
        if d == prev + timedelta(days=1):
            prev = d
        else:
            ranges.append((start, prev))
            start = d
            prev = d
    ranges.append((start, prev))
    return ranges


def _daily_weekdays(start: date, end: date) -> frozenset[date]:
    """All weekdays (Mon=0 .. Fri=4) in [start, end] inclusive."""
    out: set[date] = set()
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.add(cur)
        cur += timedelta(days=1)
    return frozenset(out)


def _weekly_fridays(start: date, end: date) -> frozenset[date]:
    """All Fridays in [start, end] inclusive."""
    out: set[date] = set()
    days_to_friday = (4 - start.weekday()) % 7
    cur = start + timedelta(days=days_to_friday)
    while cur <= end:
        out.add(cur)
        cur += timedelta(days=7)
    return frozenset(out)


def _monthly_last(start: date, end: date) -> frozenset[date]:
    """Last weekday of each calendar month overlapping [start, end]."""
    out: set[date] = set()
    cur_month = (start.year, start.month)
    end_month = (end.year, end.month)
    while cur_month <= end_month:
        year, month = cur_month
        last_day = monthrange(year, month)[1]
        d = date(year, month, last_day)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        if start <= d <= end:
            out.add(d)
        cur_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return frozenset(out)
