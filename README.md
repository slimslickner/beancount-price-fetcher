# beancount-price-fetcher

Scan a Beancount ledger for missing commodity prices and backfill them
via [yfinance](https://github.com/ranaroussi/yfinance). Writes results
to per-symbol price files (`prices/SPY.beancount`, `prices/AAPL.beancount`,
...) that you `include` from your main ledger.

## Install

```bash
uv sync                  # create .venv + install deps + generate uv.lock
```

## Quick start

```bash
# 1. See what would be fetched (no network calls)
uv run beanprices list-missing --ledger path/to/main.beancount

# 2. Preview a fetch (no files written)
uv run beanprices fetch --ledger path/to/main.beancount --dry-run

# 3. Actually fetch and write
uv run beanprices fetch --ledger path/to/main.beancount --prices-dir prices
```

Then add to your main ledger:

```beancount
include "prices/*.beancount"
```

## CLI commands

### `list-missing`

Print a table of commodities with their missing-date counts and ranges.
No network calls — safe to run as a sanity check.

```bash
uv run beanprices list-missing --ledger main.beancount \
    [--commodity SPY] \
    [--since 2024-01-01] \
    [--default-frequency daily|weekly-friday|monthly-last]
```

### `fetch`

Run the full pipeline: analyze → fetch from yfinance → write per-symbol files.

```bash
uv run beanprices fetch --ledger main.beancount --prices-dir prices \
    [--dry-run] \
    [--threads 4] \
    [--retries 3] \
    [--default-frequency daily] \
    [--commodity SPY] \
    [--since 2024-01-01]
```

Exits non-zero if any ticker fails after all retries — safe for cron/CI.
Use `--dry-run` to preview without touching the network or files.

### `migrate-dated-prices`

One-time conversion of bean-price's dated price files
(`prices/prices-YYYY-MM-DD.bean` and `prices/prices-YYYY-MM-DD.gen.bean`)
to the per-symbol layout this tool expects. Reads every dated file,
groups Price directives by commodity, writes per-symbol files, and
moves the originals to `prices/_archive_dated/` (NOT deleted — rollback
for free).

```bash
uv run beanprices migrate-dated-prices --prices-dir prices [--dry-run]
```

## Commodity metadata

Ticker mapping is read from `Commodity` directive metadata, bean-price style:

```beancount
2000-01-01 commodity AAPL
  price: "USD:yahoo/AAPL"           ; required
  price-frequency: "weekly-friday"  ; optional override
  price-start-date: 2018-01-01      ; optional override
```

The `price` metadata format is `CURRENCY:source/TICKER`. `CURRENCY`
becomes the quote currency on the resulting `Price` directive,
`TICKER` is the yfinance symbol. The `source` segment (`yahoo` here)
is ignored — only yfinance is ever used.

Per-commodity overrides:
- `price-frequency`: `daily` (default) | `weekly-friday` | `monthly-last`
- `price-start-date`: backfill further back than the first transaction

Commodities with no `Commodity` directive at all use the commodity code
as the ticker and the ledger's operating currency as the quote currency.

## "Held" definition

A commodity counts as "held" (i.e. needs prices) when it has cost-basis
inventory OR a non-base-currency position in an Assets or Liabilities
account. Income, Expense, and Equity flows are excluded.

A commodity with a buy/sell/rebuy history gets **multiple disjoint
`HeldPeriod` entries** — e.g. AAPL bought on 2021-01-15, sold on
2022-06-30, rebought on 2023-03-01 produces two periods. The fetcher
fills prices for both windows, skipping the gap.

## Library API

```python
from beancount_price_fetcher import ledger, requirements, fetcher, writer

# 1. Analyze the ledger
analysis = ledger.analyze_ledger("main.beancount")
# analysis.held_periods: dict[str, list[HeldPeriod]] (multi-period aware)
# analysis.metadata: dict[str, CommodityMetadata]
# analysis.existing_prices: dict[str, set[date]]
# analysis.operating_currencies, analysis.today

# 2. Compute what to fetch
reqs = requirements.compute_requirements(
    analysis.held_periods,
    analysis.existing_prices,
    analysis.metadata,
)
# reqs: list[PriceRequirement] -- one per commodity with non-empty missing-dates

# 3. Fetch (multi-threaded, with retry/backoff)
price_fetcher = fetcher.PriceFetcher(threads=4, retries=3)
successes, failures = price_fetcher.fetch_all(reqs)

# 4. Write per-symbol files
price_writer = writer.PriceWriter(prices_dir="prices")
for req in reqs:
    prices = [p for p in successes if p.commodity == req.commodity]
    price_writer.write_commodity(req.commodity, prices)
```

## Configuration

No config file. Global defaults live in `src/beancount_price_fetcher/constants.py`:

| Constant | Default |
|---|---|
| `DEFAULT_THREAD_COUNT` | 4 |
| `DEFAULT_RETRY_COUNT` | 3 |
| `DEFAULT_FREQUENCY` | `Frequency.DAILY` |

Override via CLI flags or by passing constructor args to `PriceFetcher` /
`compute_requirements`.

## Project layout

```
src/beancount_price_fetcher/
├── __init__.py
├── constants.py     # DEFAULT_THREAD_COUNT, DEFAULT_FREQUENCY, ...
├── models.py        # dataclasses + Frequency enum
├── ledger.py        # analyze_ledger, held-period computation
├── requirements.py  # compute_requirements (multi-period aware)
├── writer.py        # append_and_sort, PriceWriter, parse_price_file
├── migrate.py       # one-time dated-files -> per-symbol migration
├── fetcher.py       # threaded yfinance fetch with tenacity retry
└── cli.py           # click CLI entry point

tests/                # 100 tests across 8 modules
tests/fixtures/example.beancount    # shared fixture covering all edge cases
```

## License

MIT. See [LICENSE](LICENSE).