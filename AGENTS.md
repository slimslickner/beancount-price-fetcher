# AGENTS.md

beancount-price-fetcher: scan a Beancount ledger for missing commodity prices and backfill via yfinance. Standalone CLI + library, separate from `beancount-plugins`.

Use `uv` for all actions.

## Quick start

```bash
uv sync                          # install deps + generate .venv
uv run pytest                    # run all tests
uv run beanprices --help          # show CLI
uv run beanprices list-missing --ledger path/to/main.beancount
uv run beanprices fetch --ledger path/to/main.beancount --dry-run
uv run beanprices fetch --ledger path/to/main.beancount --prices-dir prices
```

## Workflow

### Required checks before committing

Every time you make changes to Python files, run all three — must complete with zero errors:

```bash
uv run ruff format              # format
uv run ruff check               # lint
uv run mypy src/beancount_price_fetcher    # type-check (strict)
uv run pytest tests/           # tests must still pass
```

Fix anything that fails; do not commit with any of these in a failing state.

### Adding code

1. Update or add tests in `tests/` (test-first per component, matching plan §5).
2. Run `uv run pytest tests/path/to/test_file.py` to confirm new tests fail (impl missing).
3. Implement in `src/beancount_price_fetcher/`.
4. Re-run pytest; iterate until green.
5. Run all four commands above; fix until clean.

### Conventions (from plan §2.3–2.4)

- **Typing**: every signature fully typed; `mypy --strict` is the bar.
- **Models** (`models.py`): `@dataclass(slots=True, frozen=True)` for value objects. This is intentional divergence from beancount's own `NamedTuple` style — our internal types, not beancount directives.
- **Decimal** (not float) for all price amounts.
- **`datetime.date`** (not datetime) for all dates.
- **`Frequency` enum** for frequency setting — parsed once, compared as enum, never as bare strings.
- **Logging**: stdlib `logging`, one logger per module (`logger = logging.getLogger(__name__)`). CLI is the only place that calls `logging.basicConfig`; library imports don't hijack caller logging.
- **No bare `except`** — catch specific exceptions; log or re-raise.
- **No comments unless asked.**
- **Docstrings**: Google style (`Args:`/`Returns:`/`Raises:`) on public functions. Private helpers (`_leading_underscore`) can have a one-line docstring or none.
- **Naming**: `snake_case` for fns/vars/modules, `PascalCase` for classes/dataclasses/enums, `UPPER_SNAKE_CASE` for module-level constants.

## Architecture

```
src/beancount_price_fetcher/
├── __init__.py
├── constants.py     # module-level defaults: DEFAULT_THREAD_COUNT, DEFAULT_FREQUENCY, ...
├── models.py         # dataclasses + Frequency enum: HeldPeriod, CommodityMetadata,
│                     # PriceRequirement, FetchedPrice
├── ledger.py        # load ledger, compute held periods (multi-period aware),
│                     # extract existing prices, parse commodity metadata
├── requirements.py  # compute missing-date requirements per commodity;
│                     # supports multi-period via union of dates within all
│                     # held periods, minus existing
├── writer.py        # append-and-sort per-symbol price files; dedup;
│                     # render_price_line + parse_price_file + PriceWriter
├── migrate.py       # one-time: dated files -> per-symbol; verify-and-archive
├── fetcher.py       # yfinance threaded fetch; tenacity retry/backoff;
│                     # per-ticker failure isolation
└── cli.py           # click CLI: list-missing, fetch, migrate-dated-prices

tests/
├── fixtures/example.beancount    # shared fixture (cost basis + non-base currency
│                                  # + price-frequency override + price-start-date
│                                  # override + no-Commodity-directive fallback +
│                                  # overlapping & closed-out holdings)
├── test_models.py
├── test_ledger.py
├── test_requirements.py
├── test_writer.py
├── test_migrate.py
├── test_fetcher.py
├── test_cli.py
└── test_e2e.py
```

## Key design decisions (locked in)

| Decision | Value |
|---|---|
| Default frequency | **daily** (per-commodity override via `price-frequency` metadata) |
| "Held" definition | **cost-basis + non-base-currency only** (Income/Expense/Equity flows excluded) |
| Retry policy | **3 attempts, exp 1s→10s, cap 30s** via tenacity |
| Python version | **3.13** |
| Multi-period holdings | **yes** — commodity can have multiple disjoint `HeldPeriod` entries (bought/sold/rebought produces two periods). `PriceRequirement` covers all of them via `min_date`/`max_date` and `missing_dates` (union of dates in all periods, minus existing). |
| Configuration | **no config file** — global defaults in `constants.py`, CLI flags override |
| Output layout | per-symbol files (`prices/SPY.beancount`, `prices/AAPL.beancount`, ...) |

## Beancount commodity metadata convention

Ticker mapping is read from `Commodity` directive metadata, bean-price style:

```
2000-01-01 commodity AAPL
  price: "USD:yahoo/AAPL"
  price-frequency: "weekly-friday"     ; optional override
  price-start-date: 2020-01-01        ; optional override
```

The `CURRENCY:source/TICKER` format: `CURRENCY` becomes the quote currency on the Price directive, `TICKER` is the yfinance symbol.

## Plan reference

Full design rationale: `docs/plans/beancount-price-fetcher.md`. Read it before making architectural changes.