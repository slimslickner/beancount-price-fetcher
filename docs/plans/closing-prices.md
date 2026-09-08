# Plan: Ensure only end-of-day (closing) prices are pulled from yfinance

## 1. Problem

`beancount-price-fetcher` is currently writing **mid-day (intraday)** prices to the ledger when the fetch window includes the current open trading day.

This is invalid for Beancount:

- Beancount requires a single effective price per `(commodity, date)`.
- A mid-day snapshot for "today" is not a meaningful settlement price —
  the price will drift from 9:30 AM to 4:00 PM ET, and the same directive
  re-fetched 5 minutes later would have a different value.
- The norm — and what makes beancount's cost-basis / unrealized-gains
  math reproducible across runs — is the official closing price of each
  trading day (the same value mutual-fund NAVs are struck at).

The user-reported symptom ("pulls prices of active stocks/ETFs mid-day")
matches the documented behavior of `yfinance.Ticker.history()` exactly.

## 2. Root cause

The fetcher calls (in `src/beancount_price_fetcher/fetcher.py`):

```python
yfinance.Ticker(ticker).history(
    start=start.isoformat(),
    end=end_excl.isoformat(),
    auto_adjust=False,
    actions=False,
)
```

yfinance `history()` is documented to return intraday data for the
current open trading day at the daily interval. Empirically confirmed:

```
$ python -c "from yfinance import Ticker; ..."
today is: 2026-09-08 weekday: 1 (Tuesday, 12:16 EDT — market open)
yfinance.Ticker('SPY').history(start=..., end=today+1, interval='1d', ...)
                                 Close
Date
2026-08-31 ...                 767.049988
2026-09-01 ...                 761.780029
...
2026-09-04 ...                 770.190002
2026-09-08 ...                 766.989990   ← intraday snapshot, NOT close
```

The September 8 row's "Close" is the running intraday price (12:16 PM ET),
not the 4:00 PM ET close. This row is what currently gets written into
`prices/SPY.bean` for date `2026-09-08`.

For completed trading days, the daily-bar "Close" *is* the closing price
— that part of the implementation is correct. The bug is specific to
"today, market still open."

### Contributing factors (in current code)

1. **No explicit `interval="1d"`** — `yfinance` defaults to `"1d"`, so this
   is correct *today* but relying on the default is fragile across yfinance
   versions. (yfinance has changed interval semantics in past releases.)
2. **No upper bound on the fetch range** — `PriceRequirement.max_date` is
   `max(p.last for p in periods)`. For an open holding period that's
   `analysis.today`, which (when passed to yfinance as `end = today + 1`)
   explicitly requests today's bar.
3. **No defensive filter in `_dataframe_to_prices`** — every row from
   yfinance is accepted, including today's intraday row.
4. **No CLI escape hatch** — users have no way to say "fetch up to and
   including today" or "fetch only through yesterday" without editing
   source.
5. **Timezone-aware date extraction is correct but undocumented** —
   `pd.Timestamp(idx).date()` on a tz-aware timestamp returns the
   exchange-local date (yfinance returns midnight ET for US stocks), which
   is what we want. Worth pinning with a test so a future yfinance
   upgrade that switches to UTC timestamps doesn't silently break date
   alignment.

## 3. Design: a three-layer fix

The fix is layered — each layer is independently correct, and together
they make mid-day prices impossible under any combination of clock
timing, market hours, and yfinance version.

### Layer 1 — `interval="1d"` passed explicitly

In `_fetch_history_with_retry`, add `interval="1d"` to the
`yfinance.Ticker.history(...)` kwargs. The default is already `"1d"`, but
explicit > implicit for a documented invariant ("daily closes only").

### Layer 2 — `_dataframe_to_prices` drops today's row (hard invariant)

The single most important change: **`_dataframe_to_prices` unconditionally
drops any row whose date equals `date.today()`** (local system date).
This is the actual safeguard.

- Local system date matches the user's mental model — when a user writes
  `2025-09-12 price SPY ...` in their ledger, "today" means their local
  calendar day, not the exchange's.
- The function takes `today: date | None = None` (defaulting to
  `date.today()`) so tests can inject a frozen date via `freezegun` or
  an explicit argument.
- Comment-style justification: a row stamped with today's calendar date
  is, by construction, either (a) the current open-day intraday snapshot
  (unsafe) or (b) the day's actual close after 4 PM ET (safe but
  indistinguishable from case (a) without per-exchange clock logic we
  don't want to ship). Excluding both is the simpler, safer rule.

This invariant alone closes the bug. The next two layers are
defense-in-depth and ergonomics.

### Layer 3 — Cap the yfinance `end` parameter

Pass `end = min(requested_max_date + 1 day, today + 1 day)` to yfinance.
This means we never request a date range that extends past today.

- yfinance's `end` is exclusive (per its docs: "end: last data point =
  end − 1 day"), so `end = today + 1 day` *includes* today.
- Capping at `today + 1 day` means we don't waste network/CPU fetching
  future-dated bars yfinance might hallucinate or refuse.

Combined with Layer 2, this guarantees today's row is requested (so the
filter can drop it predictably) and never a future-dated row is.

### Layer 4 — Optional CLI flags for power users

Add two flags to the `fetch` command:

- **`--end-date YYYY-MM-DD`** (default: today). Caps the yfinance `end`
  parameter to this date (exclusive: +1 day passed to yfinance). Useful
  for reproducible cron runs (`--end-date "$(date -d yesterday +%F)"`)
  and for backfilling on a date that isn't today.

- **`--include-today`** (default: off). When set, **bypasses** the
  Layer 2 filter for `date.today()` rows, with a logged warning at
  `WARNING` level ("you asked for today's price; this will be an intraday
  snapshot if the market is still open — re-run after market close for
  the actual close"). Off by default; opt-in only for users who know
  what they're doing.

These are non-defaults so existing behavior changes only in the safety
direction, never in the unsafe direction.

## 4. Why exclude `today` rather than check market-close time

Considered alternatives and why each is worse:

| Approach | Verdict |
|---|---|
| **Exclude `today` always** (this plan) | Simple, safe, exchange-agnostic. Loses today's close when run after 4 PM ET — user re-runs the next morning, picks it up. |
| Include today only if `now()` > 4 PM ET | Breaks for non-US markets (London, Tokyo) at the wrong clock time. Requires per-exchange calendar logic. Fragile. |
| Include today only if today is a weekday | Same intraday problem if the user runs mid-day on a weekday. |
| Fetch with a forced retry / wait until close | Adds latency to the CLI; doesn't compose with cron. |

The "lose today's close" failure mode is loud (the user can see in
`list-missing` that today is missing and re-run) and recoverable on the
next run. The "include a mid-day price" failure mode is silent and
contaminates every downstream cost-basis / gains calculation.

## 5. Changes — file by file

### `src/beancount_price_fetcher/fetcher.py`

1. In `_fetch_history_with_retry`, add `interval="1d"` to the
   `yfinance.Ticker.history(...)` call.
2. In `_dataframe_to_prices`, accept a new parameter
   `today: date | None = None` (default: `date.today()`). After building
   `out`, filter out any `FetchedPrice` whose `date == today`. Update
   the docstring accordingly.
3. In `fetch_one`, accept `today: date | None = None` and pass it through
   to `_dataframe_to_prices`. Also clamp the `end_excl` value to
   `min(end_excl, today + timedelta(days=1))` before passing to yfinance
   (so we never request past today, regardless of `req.max_date`).
4. `PriceFetcher.fetch_all` accepts `today: date | None = None` and
   threads it into every `fetch_one` call. CLI sets it explicitly via
   `--end-date` / system clock; library callers default to today.

### `src/beancount_price_fetcher/cli.py`

1. Add two options to the `fetch` command:
   - `--end-date` (`click.DateTime(formats=["%Y-%m-%d"])`): caps the
     effective `today` used in `fetch_all`. Passed through to
     `fetcher.fetch_all(reqs, dry_run=False, today=...)`.
   - `--include-today / --no-include-today` (`is_flag`, default False):
     when False, the Layer 2 filter is active; when True, the filter is
     disabled and a `logger.warning(...)` is emitted on the CLI logger
     at fetch start.
2. In `fetch_all` invocations, pass `today=...` to the fetcher.
3. Echo the effective `--end-date` in the run summary line so the user
   can see what was used (`Fetched N prices through YYYY-MM-DD`).

### `src/beancount_price_fetcher/__init__.py`

No changes needed (no exported constants from this fix).

### Tests

Per AGENTS.md "Adding code" workflow: tests first, then implementation.

**`tests/test_fetcher.py`** — add:

1. `test_dataframe_to_prices_drops_today_row`: build a DF with rows
   `[2024-01-02, 2024-01-03 (= today)]`, freeze time to
   `2024-01-03`, call `_dataframe_to_prices(df, ..., today=date(2024,1,3))`,
   assert the 2024-01-03 row is dropped, the 2024-01-02 row kept.

2. `test_dataframe_to_prices_keeps_today_when_disabled`: same fixture,
   but call with `today=date(2024,1,2)` (one day earlier than the today
   row) — the 2024-01-03 row should be kept because "today" in the
   function is the 2nd, not the 3rd.

3. `test_fetch_one_passes_explicit_daily_interval`: mock
   `yfinance.Ticker.history`, call `fetch_one`, assert the mock was
   called with `interval="1d"` in its kwargs.

4. `test_fetch_one_drops_today_from_results`: mock returns a DF
   spanning yesterday + today, freeze time to today, assert the result
   list excludes today.

5. `test_fetch_one_caps_end_at_today`: mock returns a DF; build a
   `PriceRequirement` whose `max_date` is in the future; call `fetch_one`
   with `today=date.today()`; assert the yfinance call's `end` kwarg
   is `today + 1 day` (not the future date).

6. `test_fetch_one_include_today_flag_overrides_filter`: when
   `include_today=True` is plumbed through, today's row is kept.
   (Implementation: either a `today: date | None = None` arg where
   `None` means "include all", or an `include_today: bool` flag.
   Pick one; recommend `today is None` to mean "filter disabled.")

7. `test_dataframe_to_prices_handles_tz_aware_index`: build a DF with
   index `["2025-09-12 00:00:00-04:00"]`; assert extracted date is
   `2025-09-12` (not 2025-09-13). This pins the timezone-invariant
   that today's fix relies on.

8. `test_fetch_all_threads_today_into_each_call`: in
   `PriceFetcher.fetch_all(reqs, today=date(...))`, mock returns
   intraday-snapshot rows; assert that every (ticker, today) pair was
   dropped, regardless of thread.

**`tests/test_cli.py`** — add:

9. `test_cli_fetch_drops_today_by_default`: with `freeze_time("2024-06-03")`,
   mock returns a row for 2024-06-03, run `beanprices fetch`, assert
   the output for SPY does not contain a 2024-06-03 Price directive.

10. `test_cli_fetch_include_today_warns`: with
    `--include-today`, a row for today is written; assert the warning
    appears in stderr/log output.

11. `test_cli_fetch_end_date_caps_window`: with `--end-date 2024-06-01`
    and freeze_time("2024-06-03"), assert yfinance was called with
    `end = "2024-06-02"` (end + 1 day).

12. `test_cli_list_missing_unchanged`: `list-missing` is unaffected
    (sanity check; this is a fetcher-only change).

### Documentation

- **`README.md`**: add a one-line note in the `fetch` command example
  explaining that today's price is excluded by default (always fetch
  tomorrow's run for today's close; use `--include-today` to override
  with a logged warning).
- **`docs/plans/beancount-price-fetcher.md`**: update §4.4 with a
  parenthetical noting that the fetcher excludes today's bar to avoid
  intraday snapshots.

## 6. Out of scope (explicit non-goals)

- **No per-exchange market calendar**. We don't know whether 4 PM ET,
  4:30 PM London, or 3 PM Tokyo is "close" for a given ticker. The
  "exclude today" rule is intentionally ignorant of all that.
- **No waiting / retrying for today's close**. We don't sleep until
  4:01 PM ET and re-fetch. Cron users can schedule a second run.
- **No changes to `requirements.py` / `ledger.py` / `writer.py`**. The
  bug is fetcher-side; other modules are correct.
- **No new dependencies**. `freezegun` is already a dev-dep for
  test-time freezing.

## 7. Migration / rollout

No data migration needed. The fix is forward-looking:

- Existing per-symbol files are unchanged on disk.
- A re-run of `beanprices fetch` after this change will simply not add
  a Price directive for today (if it would have done so before). Any
  *previously written* today-row directive is left in place — fixing
  historical wrong prices is a separate manual cleanup the user can do
  with `bean-format` or by hand.
- Recommend: drop any existing intraday row before deploying, but
  don't gate the release on it.

## 8. Resolved decisions

- **Flag name: `--include-today`** (not `--allow-intraday`). Locked in.
- **No yfinance short-circuit** when `req.max_date < today`. The fetcher
  always makes the call; the Layer 2 filter handles today's row uniformly.
  Adding a short-circuit would create two code paths with the same effect
  for a small network saving — not worth the extra tests.
- **Half-trading days / early closes / market holidays**: no special
  handling. yfinance's daily bar for an early-close day carries the
  early close as its Close; holidays are simply absent from the result
  set, which is already correct (no Price directive needed for a
  non-trading date).

## 9. Build order

1. Write the new tests in `tests/test_fetcher.py` and `tests/test_cli.py`
   (per AGENTS.md "test-first"). Confirm they fail against current code.
2. Implement fetcher.py changes (Layers 1, 2, 3).
3. Implement CLI flag plumbing (Layer 4).
4. Re-run all tests; iterate until green.
5. Run the four required checks (ruff format, ruff check, mypy strict,
   pytest) per AGENTS.md.
6. Update README and the original plan doc's §4.4.