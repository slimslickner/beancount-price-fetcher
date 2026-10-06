"""Threaded yfinance fetching with retry/backoff.

One ``yf.Ticker(ticker).history(start=, end=)`` call per commodity (not per
date) is the main API-efficiency lever. Multiple tickers are fetched in
parallel via ``ThreadPoolExecutor`` since yfinance is I/O-bound.

Per-ticker retry uses ``tenacity`` exponential backoff (1s → 10s, cap 10s,
max 3 attempts by default). Failures for one ticker never abort the batch
-- they're collected as ``(PriceRequirement, Exception)`` tuples and
returned alongside successes.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import yfinance
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .constants import DEFAULT_RETRY_COUNT, DEFAULT_THREAD_COUNT
from .models import FetchedPrice, PriceRequirement

logger = logging.getLogger(__name__)


def _dataframe_to_prices(
    df: pd.DataFrame,
    commodity: str,
    quote_currency: str,
    *,
    today: date | None = None,
) -> list[FetchedPrice]:
    """Convert a yfinance ``history()`` DataFrame to ``FetchedPrice`` rows.

    The index is a ``DatetimeIndex`` (timestamps); we keep only the date
    portion. Missing or NaN Close values are skipped.

    Args:
        df: yfinance ``history()`` result.
        commodity: Commodity code for the output rows.
        quote_currency: Quote currency for the output rows.
        today: If non-None, any row whose date equals ``today`` is dropped.
            This is the closing-prices safeguard: yfinance's daily bar for
            the current open trading day carries the running intraday
            snapshot as its ``Close``, not the day's actual close. Excluding
            it here keeps beancount's Price directives end-of-day only.
            None means no filter (legacy behavior).
    """
    if df is None or df.empty or "Close" not in df.columns:
        return []
    out: list[FetchedPrice] = []
    for idx, row in df.iterrows():
        close = row.get("Close")
        if pd.isna(close):
            continue
        d = pd.Timestamp(idx).date()
        if today is not None and d == today:
            continue
        out.append(
            FetchedPrice(
                commodity=commodity,
                quote_currency=quote_currency,
                date=d,
                price=Decimal(str(close)),
            )
        )
    return out


def _fetch_history_with_retry(ticker: str, start: date, end: date, retries: int) -> pd.DataFrame:
    """Call ``yf.Ticker(ticker).history(start, end)`` with retry/backoff."""

    @retry(
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def _do() -> pd.DataFrame:
        end_excl = end + timedelta(days=1)
        return yfinance.Ticker(ticker).history(
            start=start.isoformat(),
            end=end_excl.isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=False,
        )

    return _do()


def fetch_one(
    req: PriceRequirement,
    retries: int = DEFAULT_RETRY_COUNT,
    *,
    today: date | None = None,
    end_date: date | None = None,
) -> tuple[list[FetchedPrice], Exception | None]:
    """Fetch all missing prices for one ``PriceRequirement``.

    Calls yfinance for the full ``[min_date, max_date]`` range, then filters
    down to the originally-missing dates.

    Args:
        req: The price requirement to fulfill.
        retries: Per-call retry attempts (via tenacity).
        today: If non-None, drop any fetched price whose date equals
            ``today`` (the closing-prices safeguard — see
            ``_dataframe_to_prices``). None means no filter.
        end_date: If non-None, the yfinance ``end`` parameter is capped at
            ``min(req.max_date, end_date) + 1 day``. None means use
            ``req.max_date + 1 day`` (legacy behavior). yfinance's ``end``
            is exclusive, so this is an *inclusive* upper bound on the
            fetched range.

    Returns:
        ``(prices, None)`` on success (possibly empty if no data was
        available, e.g. weekend / outside trading hours).
        ``([], exception)`` if all retries were exhausted; never raises.
    """
    effective_end = req.max_date if end_date is None else min(req.max_date, end_date)
    try:
        df = _fetch_history_with_retry(req.ticker, req.min_date, effective_end, retries)
    except Exception as exc:
        logger.warning("fetch failed for %s after %d retries: %s", req.ticker, retries, exc)
        return [], exc
    all_prices = _dataframe_to_prices(df, req.commodity, req.quote_currency, today=today)
    missing = req.missing_dates
    return [p for p in all_prices if p.date in missing], None


@dataclass(slots=True)
class PriceFetcher:
    """Batch fetcher: runs all requirements in a thread pool.

    Failures are isolated per-ticker; a failure on one symbol never
    prevents the others from completing.
    """

    threads: int = DEFAULT_THREAD_COUNT
    retries: int = DEFAULT_RETRY_COUNT

    def fetch_all(
        self,
        requirements: list[PriceRequirement],
        *,
        dry_run: bool = False,
        today: date | None = None,
        end_date: date | None = None,
    ) -> tuple[list[FetchedPrice], list[tuple[PriceRequirement, Exception]]]:
        """Fetch all requirements in parallel; return (successes, failures).

        Args:
            requirements: Per-commodity requirements to fulfill.
            dry_run: If True, skip the actual fetch and return empty results.
            today: Forwarded to ``fetch_one``; see ``fetch_one`` for
                semantics. ``None`` (default) preserves legacy behavior
                (no today-filter).
            end_date: Forwarded to ``fetch_one``; see ``fetch_one`` for
                semantics. ``None`` (default) preserves legacy behavior
                (yfinance end == req.max_date + 1 day).
        """
        if dry_run:
            logger.info("dry run: skipping %d requirements", len(requirements))
            return [], []
        successes: list[FetchedPrice] = []
        failures: list[tuple[PriceRequirement, Exception]] = []
        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            future_to_req = {
                pool.submit(fetch_one, req, self.retries, today=today, end_date=end_date): req
                for req in requirements
            }
            for future in as_completed(future_to_req):
                req = future_to_req[future]
                try:
                    prices, exc = future.result()
                except Exception as exc:
                    logger.error("fetch future raised for %s: %s", req.ticker, exc)
                    failures.append((req, exc))
                    continue
                if exc is not None:
                    failures.append((req, exc))
                    continue
                successes.extend(prices)
        return successes, failures
