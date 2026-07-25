"""Ledger analysis: load ledger, extract metadata, held periods, existing prices.

This is the only module that imports beancount's loosely-typed API; everything
past this module's public functions is fully typed.

A commodity can have MULTIPLE disjoint ``HeldPeriod`` entries — e.g. a
position that's bought, fully sold, then rebought produces two periods.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from beancount.core import data
from beancount.loader import load_file

from .models import CommodityMetadata, Frequency, HeldPeriod

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class LedgerAnalysis:
    """Bundle of everything ``analyze_ledger`` extracts from a ledger."""

    metadata: dict[str, CommodityMetadata] = field(default_factory=dict)
    held_periods: dict[str, list[HeldPeriod]] = field(default_factory=dict)
    existing_prices: dict[str, set[date]] = field(default_factory=dict)
    operating_currencies: frozenset[str] = field(default_factory=frozenset)
    today: date = field(default_factory=date.today)


def analyze_ledger(path: str | Path, today: date | None = None) -> LedgerAnalysis:
    """Load a beancount ledger and extract all relevant analysis in one pass.

    Args:
        path: Path to the main beancount file (may have ``include`` directives).
        today: Reference date for "is the commodity still held?" If None,
            uses the system ``date.today()``. Tests should freeze this via
            ``freezegun``.

    Returns:
        A ``LedgerAnalysis`` with metadata, held periods, and existing prices.

    Raises:
        FileNotFoundError: If the path doesn't exist.
    """
    path = Path(path)
    if not path.exists():
        msg = f"Ledger file not found: {path}"
        raise FileNotFoundError(msg)

    entries, errors, options = load_file(str(path))
    if errors:
        for err in errors[:5]:
            logger.warning("ledger parse issue: %s", err.message)

    ref_today = today if today is not None else date.today()
    operating_currencies = frozenset(options.get("operating_currency", ["USD"]))

    return LedgerAnalysis(
        metadata=extract_commodity_metadata(entries),
        held_periods=compute_held_periods(entries, operating_currencies, ref_today),
        existing_prices=extract_existing_prices(entries),
        operating_currencies=operating_currencies,
        today=ref_today,
    )


def extract_commodity_metadata(entries: list[Any]) -> dict[str, CommodityMetadata]:
    """Build per-commodity ``CommodityMetadata`` from ``Commodity`` directives.

    Parses the bean-price convention ``price: "CURRENCY:source/TICKER"`` to
    extract ticker and quote currency. Recognised optional override keys:
    ``price-frequency`` (Frequency enum value) and ``price-start-date``
    (datetime.date).
    """
    out: dict[str, CommodityMetadata] = {}
    for entry in entries:
        if not isinstance(entry, data.Commodity):
            continue
        commodity = entry.currency
        meta = entry.meta or {}

        price_str = meta.get("price")
        if price_str is not None:
            quote_currency, ticker = _parse_price_string(str(price_str))
        else:
            quote_currency, ticker = "USD", commodity

        frequency = _parse_frequency(meta.get("price-frequency"))
        start_date = meta.get("price-start-date")
        if start_date is not None and not isinstance(start_date, date):
            start_date = None

        out[commodity] = CommodityMetadata(
            commodity=commodity,
            ticker=ticker,
            quote_currency=quote_currency,
            frequency=frequency,
            price_start_date=start_date,
        )
    return out


def compute_held_periods(
    entries: list[Any],
    operating_currencies: frozenset[str] | set[str],
    today: date,
) -> dict[str, list[HeldPeriod]]:
    """Compute one or more ``HeldPeriod`` entries per commodity.

    A commodity is considered "held" if it has cost-basis inventory OR a
    non-base-currency position in an Assets/Liabilities account. Income,
    Expense, and Equity flows don't count.

    A new period begins when a qualifying posting transitions the
    commodity's net balance from 0 to non-zero. A period ends when the
    balance returns to 0; the period's ``last`` is then the date of that
    closing posting. If a period is still open at end-of-ledger, its
    ``last`` is ``today`` and ``is_open=True``.
    """
    # Per-commodity running net balance across QUALIFYING postings only.
    balances: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    open_starts: dict[str, date] = {}
    periods: dict[str, list[HeldPeriod]] = defaultdict(list)

    for entry in sorted(entries, key=lambda e: e.date):
        if not isinstance(entry, data.Transaction):
            continue
        for posting in entry.postings:
            units = posting.units
            if units is None or units.number is None or units.number == 0:
                continue
            if not _qualifies_as_held(posting, operating_currencies):
                continue
            comm = units.currency
            old = balances[comm]
            new = old + units.number
            balances[comm] = new
            txn_date = entry.date

            if old == 0 and new != 0:
                open_starts[comm] = txn_date
            elif old != 0 and new == 0:
                start = open_starts.pop(comm)
                periods[comm].append(HeldPeriod(first=start, last=txn_date, is_open=False))

    for comm, start in open_starts.items():
        periods[comm].append(HeldPeriod(first=start, last=today, is_open=True))

    return dict(periods)


def extract_existing_prices(entries: list[Any]) -> dict[str, set[date]]:
    """Build ``{commodity: set[date]}`` from all existing ``Price`` directives."""
    out: dict[str, set[date]] = defaultdict(set)
    for entry in entries:
        if isinstance(entry, data.Price):
            commodity = entry.currency
            out[commodity].add(entry.date)
    return dict(out)


def _parse_price_string(s: str) -> tuple[str, str]:
    """Parse ``CURRENCY:source/TICKER`` -> ``(quote_currency, ticker)``."""
    if ":" not in s:
        msg = f"price metadata {s!r} missing ':' separator"
        raise ValueError(msg)
    currency, rest = s.split(":", 1)
    if "/" not in rest:
        return currency, rest
    _, ticker = rest.rsplit("/", 1)
    return currency, ticker


def _parse_frequency(raw: Any) -> Frequency | None:
    """Convert a ``price-frequency`` metadata value to ``Frequency``, or None."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    for f in Frequency:
        if f.value == s:
            return f
    msg = f"unknown price-frequency {raw!r}; expected one of: {[f.value for f in Frequency]}"
    raise ValueError(msg)


def _qualifies_as_held(posting: Any, operating_currencies: frozenset[str] | set[str]) -> bool:
    """A posting qualifies as 'held' if cost-basis OR non-base-currency in Assets/Liab."""
    units = posting.units
    if units is None:
        return False
    if units.currency in operating_currencies:
        return False
    if posting.cost is not None:
        return True
    account: str = posting.account
    return account.startswith("Assets") or account.startswith("Liabilities")
