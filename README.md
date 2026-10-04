# beancount-price-fetcher

Scan a Beancount ledger for missing commodity prices and backfill them
via [yfinance](https://github.com/ranaroussi/yfinance). Writes results
to per-symbol price files (`prices/SPY.bean`, `prices/AAPL.bean`,
...) that you `include` from your main ledger. The extension is
configurable (default `.bean`); see `--file-extension` below.

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

# 4. Backfill descriptive metadata on commodity directives
uv run beanprices fetch-metadata --ledger path/to/main.beancount --write
```

Then add to your main ledger:

```beancount
include "prices/*.bean"
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
    [--file-extension .bean] \
    [--commodity SPY] \
    [--since 2024-01-01] \
    [--end-date 2024-12-31] \
    [--include-today]
```

Exits non-zero if any ticker fails after all retries — safe for cron/CI.
Use `--dry-run` to preview without touching the network or files.

By default, the fetcher **drops any row whose date equals today** to
avoid emitting an intraday snapshot for an open trading day — only
end-of-day closes are written. Re-run after market close (or on the
next calendar day) to capture today's close, or pass `--include-today`
to bypass the filter (logged as a warning). `--end-date` caps the
yfinance fetch window for reproducible cron runs
(`--end-date "$(date -v-1d +%F)"`).

### `fetch-metadata`

Look up descriptive metadata (name, asset class, sector, industry,
category) via yfinance and write it onto the ledger's `commodity`
directives. Prices change daily; this data is effectively static, so run
this when a new holding is added or occasionally with `--refresh`.

```bash
uv run beanprices fetch-metadata --ledger main.beancount \
    [--commodity SPY] \
    [--all] \
    [--keys yf_name,yf_asset_class,yf_sector,yf_industry,yf_category] \
    [--refresh] \
    [--write] \
    [--output-file commodities.bean] \
    [--threads 4] \
    [--retries 3]
```

Unlike `fetch`, the default is a **preview**: it prints a unified diff of
every planned edit and touches nothing. Pass `--write` to apply. This is
deliberate — the command edits hand-maintained ledger files.

- **Default scope** is held commodities (any historical or current
  `HeldPeriod`). `--all` adds every commodity with a `commodity`
  directive. Operating currencies are always skipped.
- **Add vs. update**: by default a key is added only when absent; an
  existing value (including an empty string) is left alone. With
  `--refresh`, a key whose provider value differs is replaced. Values are
  never blanked out or deleted.
- **`--keys`** limits which keys are added or updated, e.g.
  `--keys yf_sector,yf_industry --refresh` leaves hand-written `yf_name` alone.
- **No directive**: a commodity without a `commodity` directive gets one
  appended to `--output-file` (default `commodities.bean` next to the
  ledger), dated to its first held period. Add
  `include "commodities.bean"` to your ledger if it isn't already
  included.

Exit status is non-zero only when a lookup raised after all retries or a
file could not be edited safely. "Provider returned nothing" is not a
failure. Status per commodity: `filled`, `updated`, `unchanged`,
`complete`, `partial`, `not-found`, `error`.

#### Metadata keys

Written in this order when adding:

| Key | Meaning |
|---|---|
| `yf_name` | Long name, e.g. `"Vanguard Total Stock Market ETF"` |
| `yf_asset_class` | `"Equity"`, `"Bond"`, `"Cash"`, `"Crypto"`, etc. |
| `yf_sector` | Stocks only |
| `yf_industry` | Stocks only |
| `yf_category` | Funds only (Morningstar-style, e.g. `"Large Blend"`) |

Keys are namespaced `yf_` to make clear they came from yfinance and to avoid
colliding with hand-written metadata.

Routing is on yfinance's `quoteType`: equities get `yf_name`,
`yf_asset_class`, `yf_sector`, `yf_industry`; ETFs and mutual funds get
`yf_name`, `yf_category`, and `yf_asset_class` inferred from the fund
breakdown when one position is at least 80% of the total (mixed funds are
left for a human); crypto gets `yf_name` and `yf_asset_class` `Crypto`;
anything else gets `yf_name` only.

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

`fetch-metadata` adds these descriptive keys to the same directive:

```beancount
2000-01-01 commodity AAPL
  price: "USD:yahoo/AAPL"
  yf_name: "Apple Inc."
  yf_asset_class: "Equity"
  yf_sector: "Technology"
  yf_industry: "Consumer Electronics"
```

## "Held" definition

A commodity counts as "held" (i.e. needs prices) when it has cost-basis
inventory OR a non-base-currency position in an Assets or Liabilities
account. Income, Expense, and Equity flows are excluded.

A commodity with a buy/sell/rebuy history gets **multiple disjoint
`HeldPeriod` entries** — e.g. AAPL bought on 2021-01-15, sold on
2022-06-30, rebought on 2023-03-01 produces two periods. The fetcher
fills prices for both windows, skipping the gap.

## Display precision

Price amounts are quantized to each quote currency's `display_precision`
increment declared in the ledger:

```beancount
option "display_precision" "USD:0.01"
option "display_precision" "EUR:0.0001"
option "display_precision" "JPY:1"
```

With the above, USD prices are written with exactly 2 decimals, EUR
with 4, JPY as integers. Currencies not in the precision map keep full
precision. Rounding uses `ROUND_HALF_EVEN` (banker's rounding), the
same default beancount uses.

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
| `DEFAULT_FILE_EXTENSION` (in `writer.py`) | `.bean` |

Override via CLI flags or by passing constructor args to `PriceFetcher` /
`PriceWriter` / `compute_requirements`.

### Fetch behavior: append-only, no re-sort

The `fetch` command is **append-only**: it does not re-sort or re-render
existing content in a per-symbol file. New missing prices are appended at
the end in the order returned by yfinance. This means existing hand-curated
or otherwise-ordered lines are preserved verbatim.

If you want the file sorted by date after a fetch, run a downstream tool
like [bean-format](https://github.com/aktau/bean-format) over the
generated files. The `migrate-dated-prices` command sorts by date
internally (because it's writing fresh per-symbol files from scratch); the
`fetch` command does not, by design.

## Project layout

```
src/beancount_price_fetcher/
├── __init__.py
├── constants.py        # DEFAULT_THREAD_COUNT, DEFAULT_FREQUENCY, ...
├── models.py           # dataclasses + Frequency/MetadataStatus enums
├── ledger.py           # analyze_ledger, held-period computation
├── requirements.py     # compute_requirements (multi-period aware)
├── writer.py           # append_and_sort, PriceWriter, parse_price_file
├── migrate.py          # one-time dated-files -> per-symbol migration
├── fetcher.py          # threaded yfinance fetch with tenacity retry
├── metadata_fetcher.py # quoteType routing + threaded metadata lookup
├── metadata_writer.py  # in-place commodity directive text edits
└── cli.py              # click CLI entry point

tests/                # tests across 10 modules + 2 shared fixtures
tests/fixtures/example.beancount     # price-fetch fixture for all edge cases
tests/fixtures/metadata.beancount    # metadata-command fixture
```

## License

MIT. See [LICENSE](LICENSE).