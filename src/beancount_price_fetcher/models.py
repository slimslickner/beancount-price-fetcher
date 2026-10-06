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


class MetadataStatus(Enum):
    """Outcome of a metadata lookup/edit for one commodity.

    FILLED: one or more keys added.
    UPDATED: one or more existing values changed under ``--refresh``.
    UNCHANGED: looked up, nothing to add or change.
    COMPLETE: nothing missing, no lookup performed.
    PARTIAL: some selected keys still unavailable from the provider.
    NOT_FOUND: provider responded but had nothing usable.
    ERROR: lookup raised after all retries, or a file edit was refused.
    """

    FILLED = "filled"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    COMPLETE = "complete"
    PARTIAL = "partial"
    NOT_FOUND = "not-found"
    ERROR = "error"


#: A metadata value as stored on a directive. Strings are written quoted;
#: ``int``/``Decimal`` are written as bare Beancount numbers.
MetadataValue = str | int | Decimal


def metadata_value_text(value: MetadataValue) -> str:
    """Render a metadata value as bare source text (no quoting).

    Used both to write numbers unquoted and to compare a provider value
    against the string form already stored on a directive.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


@dataclass(slots=True, frozen=True)
class CommodityInfo:
    """Descriptive metadata for one commodity as returned by yfinance.

    Every field is optional: providers frequently omit sector/industry for
    funds or category for equities, and a lookup that yields nothing usable
    is not an error. ``quote_type``/``isin``/``exchange``/``currency`` are
    populated for all security types; the fund-only fields are set only for
    ETFs/mutual funds.

    ``expense_ratio`` is a decimal fraction (0.05% -> ``Decimal("0.0005")``),
    not a percentage; ``morningstar_rating`` is an integer 1-5.
    """

    name: str | None = None
    asset_class: str | None = None
    sector: str | None = None
    industry: str | None = None
    category: str | None = None
    quote_type: str | None = None
    isin: str | None = None
    exchange: str | None = None
    currency: str | None = None
    fund_family: str | None = None
    expense_ratio: Decimal | None = None
    morningstar_rating: int | None = None
    market_cap_category: str | None = None


@dataclass(slots=True, frozen=True)
class MetadataPlan:
    """Planned metadata edits for one commodity.

    ``adds`` is an ordered ``(key, value)`` tuple sequence; ``updates`` is an
    ordered ``(key, old_value, new_value)`` tuple sequence. Both are empty
    when the status is COMPLETE/UNCHANGED/PARTIAL/NOT_FOUND/ERROR.
    """

    commodity: str
    status: MetadataStatus
    adds: tuple[tuple[str, MetadataValue], ...] = ()
    updates: tuple[tuple[str, object, MetadataValue], ...] = ()
