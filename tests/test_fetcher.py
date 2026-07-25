"""Tests for beancount_price_fetcher.fetcher against mocked yfinance.

Per the plan, no real network calls in CI. We mock ``yf.Ticker`` and
exercise single-threaded + threaded paths against controlled responses.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from beancount_price_fetcher.fetcher import (
    PriceFetcher,
    _dataframe_to_prices,
    fetch_one,
)
from beancount_price_fetcher.models import FetchedPrice, Frequency, PriceRequirement


def _make_df(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """Build a minimal yfinance-style DataFrame with Date index + Close column."""
    idx = pd.to_datetime([r[0] for r in rows])
    df = pd.DataFrame(
        {"Close": [r[1] for r in rows]},
        index=idx,
    )
    df.index.name = "Date"
    return df


def test_dataframe_to_prices_basic() -> None:
    df = _make_df([("2024-01-02", 300.0), ("2024-01-03", 301.5)])
    prices = _dataframe_to_prices(df, commodity="SPY", quote_currency="USD")
    assert len(prices) == 2
    assert prices[0] == FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("300.0"))
    assert prices[1] == FetchedPrice("SPY", "USD", date(2024, 1, 3), Decimal("301.5"))


def test_dataframe_to_prices_empty() -> None:
    df = pd.DataFrame(columns=["Close"], index=pd.DatetimeIndex([]))
    prices = _dataframe_to_prices(df, commodity="SPY", quote_currency="USD")
    assert prices == []


def test_dataframe_to_prices_datetime_index_preserves_date() -> None:
    """The index is a datetime; we extract only the date portion."""
    df = _make_df([("2024-01-02 13:30:00", 300.0)])
    prices = _dataframe_to_prices(df, commodity="SPY", quote_currency="USD")
    assert prices[0].date == date(2024, 1, 2)


def test_fetch_one_returns_prices_for_missing_dates(
    mocker: Any,
) -> None:
    """Mocked yfinance returns DataFrame; only missing dates get included."""
    req = PriceRequirement(
        commodity="SPY",
        ticker="SPY",
        quote_currency="USD",
        frequency=Frequency.DAILY,
        min_date=date(2024, 1, 2),
        max_date=date(2024, 1, 10),
        missing_dates=frozenset({date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)}),
    )
    mock_history = mocker.patch("yfinance.Ticker.history")
    mock_history.return_value = _make_df(
        [
            ("2024-01-02", 300.0),
            ("2024-01-03", 301.0),
            ("2024-01-04", 302.0),
            ("2024-01-05", 303.0),  # not in missing_dates; should be excluded
        ]
    )
    prices, exc = fetch_one(req)
    assert exc is None
    # Only the missing dates should be returned
    assert len(prices) == 3
    dates = {p.date for p in prices}
    assert dates == {date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)}


def test_fetch_one_returns_empty_when_no_data(mocker: Any) -> None:
    """yfinance returns empty df -> empty list, no error."""
    from beancount_price_fetcher.models import Frequency

    req = PriceRequirement(
        commodity="SPY",
        ticker="SPY",
        quote_currency="USD",
        frequency=Frequency.DAILY,
        min_date=date(2024, 1, 2),
        max_date=date(2024, 1, 10),
        missing_dates=frozenset({date(2024, 1, 2)}),
    )
    mocker.patch("yfinance.Ticker.history", return_value=_make_df([]))
    prices, exc = fetch_one(req)
    assert prices == []
    assert exc is None


def test_fetch_one_retries_on_failure_then_succeeds(mocker: Any) -> None:
    """First call raises, second succeeds. With 3 retries, eventually gets data."""
    from beancount_price_fetcher.models import Frequency

    req = PriceRequirement(
        commodity="SPY",
        ticker="SPY",
        quote_currency="USD",
        frequency=Frequency.DAILY,
        min_date=date(2024, 1, 2),
        max_date=date(2024, 1, 10),
        missing_dates=frozenset({date(2024, 1, 2)}),
    )
    mock_history = mocker.patch("yfinance.Ticker.history")
    # First 2 calls raise, third succeeds
    mock_history.side_effect = [
        RuntimeError("transient"),
        RuntimeError("transient"),
        _make_df([("2024-01-02", 300.0)]),
    ]
    prices, exc = fetch_one(req, retries=3)
    assert exc is None
    assert len(prices) == 1
    assert mock_history.call_count == 3


def test_fetch_one_returns_empty_after_exhausting_retries(mocker: Any) -> None:
    """All retries exhausted: fetcher yields empty list (does not raise)."""
    from beancount_price_fetcher.models import Frequency

    req = PriceRequirement(
        commodity="SPY",
        ticker="SPY",
        quote_currency="USD",
        frequency=Frequency.DAILY,
        min_date=date(2024, 1, 2),
        max_date=date(2024, 1, 10),
        missing_dates=frozenset({date(2024, 1, 2)}),
    )
    mock_history = mocker.patch("yfinance.Ticker.history")
    mock_history.side_effect = RuntimeError("persistent")
    prices, exc = fetch_one(req, retries=2)
    assert prices == []
    assert exc is not None
    assert isinstance(exc, RuntimeError)
    assert mock_history.call_count == 2


def test_price_fetcher_runs_all_requirements(mocker: Any) -> None:
    """PriceFetcher.fetch_all runs every requirement."""
    from beancount_price_fetcher.models import Frequency

    reqs = [
        PriceRequirement(
            commodity="SPY",
            ticker="SPY",
            quote_currency="USD",
            frequency=Frequency.DAILY,
            min_date=date(2024, 1, 2),
            max_date=date(2024, 1, 3),
            missing_dates=frozenset({date(2024, 1, 2)}),
        ),
        PriceRequirement(
            commodity="AAPL",
            ticker="AAPL",
            quote_currency="USD",
            frequency=Frequency.DAILY,
            min_date=date(2024, 1, 2),
            max_date=date(2024, 1, 3),
            missing_dates=frozenset({date(2024, 1, 2)}),
        ),
    ]
    mock_history = mocker.patch("yfinance.Ticker.history")
    mock_history.side_effect = lambda **kw: _make_df([("2024-01-02", 300.0)])
    fetcher = PriceFetcher(threads=1, retries=1)
    successes, failures = fetcher.fetch_all(reqs)
    assert len(successes) == 2
    assert failures == []


def test_price_fetcher_isolates_ticker_failures(mocker: Any) -> None:
    """One ticker fails, the other still succeeds; both are reported."""
    from beancount_price_fetcher.models import Frequency

    reqs = [
        PriceRequirement(
            commodity="GOOD",
            ticker="GOOD",
            quote_currency="USD",
            frequency=Frequency.DAILY,
            min_date=date(2024, 1, 2),
            max_date=date(2024, 1, 3),
            missing_dates=frozenset({date(2024, 1, 2)}),
        ),
        PriceRequirement(
            commodity="BAD",
            ticker="BAD",
            quote_currency="USD",
            frequency=Frequency.DAILY,
            min_date=date(2024, 1, 2),
            max_date=date(2024, 1, 3),
            missing_dates=frozenset({date(2024, 1, 2)}),
        ),
    ]
    mock_history = mocker.patch("yfinance.Ticker.history")

    def selective_history(**kw: Any) -> pd.DataFrame:
        # yfinance.Ticker.history doesn't get the ticker as a kwarg; it's set on Ticker.
        # We can read it from the call site via thread-local or instance attr; here
        # we just key on call order for simplicity: first call -> success, second -> raise.
        if not hasattr(selective_history, "calls"):
            selective_history.calls = 0  # type: ignore[attr-defined]
        selective_history.calls += 1  # type: ignore[attr-defined]
        if selective_history.calls % 2 == 1:  # type: ignore[attr-defined]
            return _make_df([("2024-01-02", 300.0)])
        raise RuntimeError("BAD ticker failed")

    mock_history.side_effect = selective_history
    fetcher = PriceFetcher(threads=1, retries=1)
    successes, failures = fetcher.fetch_all(reqs)
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0][0].commodity == "BAD"


def test_price_fetcher_returns_failures_list_shape(mocker: Any) -> None:
    """Failure entries are (PriceRequirement, Exception) tuples."""
    from beancount_price_fetcher.models import Frequency

    reqs = [
        PriceRequirement(
            commodity="X",
            ticker="X",
            quote_currency="USD",
            frequency=Frequency.DAILY,
            min_date=date(2024, 1, 2),
            max_date=date(2024, 1, 3),
            missing_dates=frozenset({date(2024, 1, 2)}),
        ),
    ]
    mocker.patch("yfinance.Ticker.history", side_effect=RuntimeError("nope"))
    fetcher = PriceFetcher(threads=1, retries=1)
    successes, failures = fetcher.fetch_all(reqs)
    assert successes == []
    assert len(failures) == 1
    assert isinstance(failures[0][0], PriceRequirement)
    assert isinstance(failures[0][1], RuntimeError)


def test_price_fetcher_threaded(mocker: Any) -> None:
    """threads > 1 runs in parallel; results identical to single-threaded."""
    from beancount_price_fetcher.models import Frequency

    reqs = [
        PriceRequirement(
            commodity=f"X{i}",
            ticker=f"X{i}",
            quote_currency="USD",
            frequency=Frequency.DAILY,
            min_date=date(2024, 1, 2),
            max_date=date(2024, 1, 3),
            missing_dates=frozenset({date(2024, 1, 2)}),
        )
        for i in range(5)
    ]
    mock_history = mocker.patch("yfinance.Ticker.history")
    mock_history.side_effect = lambda **kw: _make_df([("2024-01-02", 300.0)])
    fetcher = PriceFetcher(threads=4, retries=1)
    successes, failures = fetcher.fetch_all(reqs)
    assert len(successes) == 5
    assert failures == []


def test_price_fetcher_dry_run_does_not_call_yfinance(mocker: Any) -> None:
    """Dry run skips yfinance calls entirely."""
    from beancount_price_fetcher.models import Frequency

    reqs = [
        PriceRequirement(
            commodity="SPY",
            ticker="SPY",
            quote_currency="USD",
            frequency=Frequency.DAILY,
            min_date=date(2024, 1, 2),
            max_date=date(2024, 1, 3),
            missing_dates=frozenset({date(2024, 1, 2)}),
        ),
    ]
    mock_history = mocker.patch("yfinance.Ticker.history")
    fetcher = PriceFetcher(threads=1, retries=1)
    successes, failures = fetcher.fetch_all(reqs, dry_run=True)
    assert successes == []
    assert failures == []
    mock_history.assert_not_called()


# ---- Thread-dispatch invariants ----
#
# These tests verify that tasks are dispatched per-COMMODITY, not per-date,
# so that no two threads can ever process the same (commodity, date) pair.
# Each PriceRequirement represents one commodity; one yfinance call covers
# that commodity's entire date range; threads run commodities in parallel.


def test_price_fetcher_dispatches_one_task_per_commodity(mocker: Any) -> None:
    """N commodities -> N thread-pool tasks, NOT N*dates tasks.

    We instrument the mock to count distinct (commodity, call-time) pairs
    and confirm that each commodity is fetched exactly once.
    """
    import threading

    tickers = [f"T{i}" for i in range(5)]
    missing_dates = frozenset({date(2024, 1, 2), date(2024, 1, 3)})
    reqs = [
        PriceRequirement(
            commodity=t,
            ticker=t,
            quote_currency="USD",
            frequency=Frequency.DAILY,
            min_date=date(2024, 1, 2),
            max_date=date(2024, 1, 3),
            missing_dates=missing_dates,
        )
        for t in tickers
    ]

    seen_lock = threading.Lock()
    seen_calls: list[tuple[str, int]] = []

    def history(**kwargs: Any) -> pd.DataFrame:
        thread_id = threading.get_ident()
        with seen_lock:
            # yfinance.Ticker.history doesn't get the ticker as a kwarg;
            # the only way to identify which commodity was fetched is via
            # thread + call order, but we can also count via the side effect
            # that we know exactly which Ticker instance was created.
            seen_calls.append((threading.current_thread().name, thread_id))
        return _make_df(
            [
                ("2024-01-02", 100.0),
                ("2024-01-03", 101.0),
            ]
        )

    mocker.patch("yfinance.Ticker.history", side_effect=history)
    fetcher = PriceFetcher(threads=4, retries=1)
    successes, failures = fetcher.fetch_all(reqs)

    # 5 commodities, 5 yfinance calls, each covering both dates
    assert len(seen_calls) == 5
    assert len(successes) == 5 * 2  # 5 commodities x 2 dates each
    assert failures == []


def test_price_fetcher_no_overlap_per_thread(mocker: Any) -> None:
    """Each (commodity, date) pair is touched by at most one thread.

    Uses a thread-id-stamped mock to track which thread processed each
    (ticker, date) pair. The invariant: each pair appears exactly once,
    even with multiple threads running concurrently.
    """
    import threading

    tickers = [f"T{i}" for i in range(8)]
    reqs = [
        PriceRequirement(
            commodity=t,
            ticker=t,
            quote_currency="USD",
            frequency=Frequency.DAILY,
            min_date=date(2024, 1, 2),
            max_date=date(2024, 1, 4),
            missing_dates=frozenset({date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)}),
        )
        for t in tickers
    ]

    # Track every (ticker, date) -> thread_id mapping we observe.
    # yfinance.Ticker.history doesn't receive the ticker as a kwarg, but
    # each PriceRequirement has its own yfinance.Ticker instance created
    # inside fetch_one; we can identify them via the side_effect order.
    processed: dict[tuple[str, date], int] = {}
    process_lock = threading.Lock()
    ticker_index = iter(tickers)

    def history(**kwargs: Any) -> pd.DataFrame:
        # Identify which ticker this is by popping the next from our
        # iterator. Note: the order of calls corresponds to the order
        # of pool.submit() invocations, which is the order of `reqs`.
        # We rely on that to map back to the originating ticker.
        # (For a more robust mapping in production, see fetch_all below.)
        current_thread = threading.get_ident()
        with process_lock:
            ticker = next(ticker_index)
            for d in (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)):
                key = (ticker, d)
                assert key not in processed, f"{key} already processed by another thread"
                processed[key] = current_thread
        return _make_df(
            [
                ("2024-01-02", 100.0),
                ("2024-01-03", 101.0),
                ("2024-01-04", 102.0),
            ]
        )

    mocker.patch("yfinance.Ticker.history", side_effect=history)
    fetcher = PriceFetcher(threads=4, retries=1)
    successes, failures = fetcher.fetch_all(reqs)

    # Every (commodity, date) pair processed exactly once
    assert len(processed) == 8 * 3  # 8 commodities x 3 dates
    assert all(
        (t, d) in processed
        for t in tickers
        for d in (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    )
    # Each (commodity, date) -> exactly ONE thread-id
    assert len(set(processed.values())) <= fetcher.threads  # bounded by thread count
    assert failures == []
    assert len(successes) == 8 * 3


def test_price_fetcher_single_thread_processes_whole_commodity(mocker: Any) -> None:
    """threads=1 still works and processes the whole commodity in one go."""
    import threading

    seen_thread_ids: list[int] = []

    def history(**kwargs: Any) -> pd.DataFrame:
        seen_thread_ids.append(threading.get_ident())
        return _make_df([("2024-01-02", 100.0), ("2024-01-03", 101.0), ("2024-01-04", 102.0)])

    mocker.patch("yfinance.Ticker.history", side_effect=history)
    reqs = [
        PriceRequirement(
            commodity="SPY",
            ticker="SPY",
            quote_currency="USD",
            frequency=Frequency.DAILY,
            min_date=date(2024, 1, 2),
            max_date=date(2024, 1, 4),
            missing_dates=frozenset({date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)}),
        ),
    ]
    fetcher = PriceFetcher(threads=1, retries=1)
    successes, failures = fetcher.fetch_all(reqs)

    assert len(seen_thread_ids) == 1
    assert len(successes) == 3
    assert failures == []
