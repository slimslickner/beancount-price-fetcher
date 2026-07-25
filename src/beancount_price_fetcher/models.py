"""Internal data models for beancount-price-fetcher.

These are immutable value objects passed between pipeline stages. We use
``@dataclass(slots=True, frozen=True)`` for memory/attribute-typo safety,
deliberately diverging from beancount's own ``NamedTuple`` directive types
(those are beancount's; these are ours).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum


class Frequency(Enum):
    """How often to require a price directive for a commodity.

    DAILY: every weekday (Mon-Fri).
    WEEKLY_FRIDAY: every Friday.
    MONTHLY_LAST: last business day of each month.
    """

    DAILY = "daily"
    WEEKLY_FRIDAY = "weekly-friday"
    MONTHLY_LAST = "monthly-last"


@dataclass(slots=True, frozen=True)
class HeldPeriod:
    """A single contiguous holding period for a commodity.

    `first` is the date the commodity entered inventory (transitioned from
    zero balance to non-zero). `last` is the date it left, or `today` if
    still held (`is_open=True`). A commodity with a buy/sell/rebuy history
    has multiple ``HeldPeriod`` entries.
    """

    first: date
    last: date
    is_open: bool


@dataclass(slots=True, frozen=True)
class CommodityMetadata:
    """Per-commodity configuration parsed from ``Commodity`` directive metadata.

    `ticker` is the yfinance symbol to fetch. `quote_currency` is the
    currency to write on the resulting Price directive. `frequency` and
    `price_start_date` are optional overrides (None means "use default").
    """

    commodity: str
    ticker: str
    quote_currency: str
    frequency: Frequency | None
    price_start_date: date | None


@dataclass(slots=True, frozen=True)
class PriceRequirement:
    """The set of price directives that need to be fetched for one commodity.

    `missing_dates` is the exact set of dates to fetch (already filtered
    against existing prices). `min_date` and `max_date` are the inclusive
    range bounds to ask yfinance for in a single ``history()`` call.
    """

    commodity: str
    ticker: str
    quote_currency: str
    frequency: Frequency
    min_date: date
    max_date: date
    missing_dates: frozenset[date]


@dataclass(slots=True, frozen=True)
class FetchedPrice:
    """A single price point returned by yfinance for a commodity/date pair."""

    commodity: str
    quote_currency: str
    date: date
    price: Decimal
