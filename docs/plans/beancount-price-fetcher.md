# Plan: `beancount-price-fetcher`

A standalone Python library/CLI that scans a Beancount ledger for missing
commodity prices across its entire history and backfills them via
`yfinance`, writing results to per-symbol price files.

This is intentionally **separate** from `beancount-plugins` — it's a
data-fetching CLI, not a beancount `plugin "..."` module.

---

## 1. Scope

- Input: a Beancount ledger (main file + includes).
- Output: `Price` directives appended to per-symbol files
  (`prices/SPY.beancount`, `prices/FXAIX.beancount`, etc.), one file per
  commodity, included into the main ledger via `include "prices/*.beancount"`.
- Data source: `yfinance` only (no need for beanprice's Source abstraction —
  keep it simple and single-purpose).
- Must support: missing-price detection across full history, threaded
  fetching, dry-run mode, per-commodity ticker mapping.

## 2. Tooling, dependencies & conventions

Be specific here so the coding agent doesn't default to whatever it
knows best — pin these choices.

### 2.1 Runtime & packaging

- **Python**: 3.11+ (use `match` statements where they genuinely clarify
  branching, e.g. frequency handling; don't force them elsewhere).
- **Package manager**: `uv` — matches the sibling `beancount-plugins`
  repo's tooling (`.python-version`, `uv.lock`). Use `uv init --lib` /
  `uv add` rather than raw `pip`/`venv`.
- **Layout**: `src/` layout —
  `src/beancount_price_fetcher/{ledger,requirements,fetcher,writer,migrate,cli,models}.py`.
  Avoids the package-shadowing footguns of a flat layout, and is what
  `uv`/`hatchling` scaffold by default.
- **Build backend**: `hatchling`, declared in `pyproject.toml`
  `[build-system]`.
- **CLI entry point**: register via `[project.scripts]` in
  `pyproject.toml` (e.g. `beanprices = "beancount_price_fetcher.cli:main"`),
  not a `if __name__ == "__main__"` shim.

### 2.2 Dependencies

Core (`[project.dependencies]`):
- `beancount>=3.0` — confirm exact import paths against the installed
  version before writing code; beancount 3.x reorganized some modules
  relative to 2.x, so don't assume 2.x-era import paths from memory.
- `yfinance>=0.2` — pin a specific known-good version in `uv.lock` and
  bump deliberately, not automatically (see earlier discussion — Yahoo's
  unofficial endpoints break yfinance periodically).
- `tenacity>=8` — for retry/backoff on `fetcher.py` network calls.
  Don't hand-roll retry loops; `tenacity`'s `@retry` decorator with
  `wait_exponential` + `stop_after_attempt` is a well-typed, one-line fit.
- `click>=8` — CLI framework. Preferred over raw `argparse` for command
  grouping (`list-missing`, `fetch`, `migrate-dated-prices` as
  subcommands) and over `typer` to avoid an extra layer of decorator
  magic on top of click that isn't needed for three subcommands.

Dev-only (`[dependency-groups.dev]` or `[tool.uv.dev-dependencies]`):
- `pytest>=8`
- `pytest-mock` — for mocking `yfinance.Ticker.history`.
- `freezegun` — for tests that depend on "today" (held-range
  calculations, default end dates). Don't let tests be flaky/date-
  dependent; freeze the clock explicitly in every test that touches
  "today."
- `ruff` — linting **and** formatting (replaces black+isort+flake8 with
  one tool; run `ruff check` and `ruff format`).
- `mypy` — type checking, strict mode (see 2.3).

Explicitly **not** a dependency: no `pyyaml`/`pydantic` — there's no
config file to parse (section 4.3), and internal data models are plain
`dataclasses`, not validated external input, so `pydantic` would be
unused weight. No direct `pandas` dependency in our own code — yfinance
returns `pandas.DataFrame`s, but convert to plain Python objects
(`date`, `Decimal`) immediately at the `fetcher.py` boundary rather than
threading pandas objects through the rest of the codebase.

### 2.3 Typing

- Every function signature fully typed — arguments and return type, no
  bare `Any` except at the narrow boundary where beancount's own
  loosely-typed API is consumed (beancount doesn't ship complete type
  stubs; wrap those calls in a thin typed function early and treat
  everything past that wrapper as fully typed).
- `mypy --strict` in CI. If beancount's lack of stubs makes strict mode
  impractical for `ledger.py` specifically, scope a narrow
  `# type: ignore[import-untyped]` at the beancount import lines, not a
  blanket `ignore_errors` for the whole module.
- Use `@dataclass(slots=True, frozen=True)` for value objects
  (`PriceRequirement`, `FetchedPrice`, `CommodityMetadata`, etc.) —
  `slots=True` for memory/attribute-typo safety, `frozen=True` since
  these are immutable records passed between pipeline stages, not
  mutated in place. Note: this is a deliberate deviation from beancount's
  own convention (beancount's core directives — `Price`, `Cost`,
  `Transaction` — are built on `typing.NamedTuple`, not `dataclass`).
  These are our own internal types, not beancount directive types, so
  there's no need to mirror that older stylistic choice — but it's a
  conscious pick, not an oversight, and worth knowing if you're
  comparing this codebase to beancount's own source.
- Use `Decimal` (not `float`) for all price amounts, matching
  beancount's own convention — never introduce float rounding error into
  price directives.
- Use `datetime.date` (not `datetime.datetime`) for all dates — prices
  are date-scoped, not time-scoped.
- Prefer `Enum` for the frequency setting (`Frequency.DAILY`,
  `Frequency.WEEKLY_FRIDAY`, `Frequency.MONTHLY_LAST`) over bare strings,
  parsed once at the ledger/metadata boundary rather than compared as
  strings throughout the codebase.

### 2.4 Style & conventions

- Formatting/linting: `ruff format` + `ruff check`, both run in CI;
  no manual style debates — whatever `ruff format` produces is correct.
- Docstrings: Google-style (`Args:`/`Returns:`/`Raises:`), on every
  public function and class. Private helpers (`_leading_underscore`)
  can have a one-line docstring or none if the name is self-explanatory.
- Naming: `snake_case` for functions/variables/modules, `PascalCase` for
  classes/dataclasses/enums, `UPPER_SNAKE_CASE` for module-level
  constants (e.g. `DEFAULT_THREAD_COUNT`, `DEFAULT_FREQUENCY`).
- Logging: stdlib `logging`, not `print`. One logger per module
  (`logger = logging.getLogger(__name__)`). CLI exposes `-v`/`-vv` to
  bump verbosity; library code never configures logging handlers itself
  (only the CLI entry point calls `logging.basicConfig`), so importing
  this as a library doesn't hijack a caller's logging setup.
- No bare `except:` — catch specific exceptions
  (`yfinance`/network errors in `fetcher.py`, `beancount` parse errors
  in `ledger.py`), and always log or re-raise, never silently swallow.
- Global defaults (thread count, retry count, default frequency) as
  module-level constants with CLI flags/constructor args overriding
  them — not magic numbers scattered inline (per the "no config file,
  but no hidden defaults either" decision from section 4.3).

## 3. Architecture

```
beancount_price_fetcher/
├── ledger.py       # load ledger, extract holdings, existing prices,
│                   # and per-commodity metadata (ticker/frequency/start)
├── requirements.py # compute which (commodity, date) pairs are needed
├── fetcher.py       # yfinance fetching, threaded
├── writer.py        # append Price directives to per-symbol files
├── cli.py            # entry point: list-missing, fetch, fetch --dry-run
└── models.py          # dataclasses: PriceRequirement, FetchedPrice, etc.
tests/
```

## 4. Component details

### 4.1 `ledger.py` — Ledger analysis

- Use `beancount.loader.load_file()` to parse the ledger (main file + all
  includes) into `entries, errors, options`.
- Build a per-commodity **held-date range**:
  - Walk `Open`/`Close` directives and postings to determine, for each
    currency held at cost or in a non-base currency, the first date it
    appears in an inventory and the last date it's still held (or today,
    if still open).
  - `beancount.core.inventory` can be used incrementally, or reuse the
    logic bean-price already has for "commodities held at a given date"
    and just run it across the whole date range instead of a single date.
- Build a set of **existing price dates per commodity** by scanning all
  `Price` directives already in the ledger (via `beancount.core.prices`
  or just filtering `entries` for `Price` type).
- Output: `dict[str, HeldRange]` and `dict[str, set[date]]`.

### 4.2 `requirements.py` — Missing price computation

- For each commodity, generate the set of **required dates**: default to
  weekdays (Mon–Fri) between first-held and last-held/today. Allow a
  config override for frequency (`daily`, `weekly-friday`, `monthly-last`)
  per commodity, since daily price entries for a buy-and-hold ETF are
  often overkill.
- `missing = required_dates - existing_dates`, per commodity.
- Collapse missing dates into **date ranges** per commodity (don't fetch
  day-by-day — yfinance `history(start=, end=)` returns a whole range in
  one call, which matters both for API efficiency and for not hammering
  Yahoo's endpoint).
- Output: `list[PriceRequirement]`, one per commodity, each holding a
  ticker, a list of missing dates, and a min/max range to fetch.

### 4.3 Ticker mapping via `Commodity` directive metadata (in `ledger.py`)

No separate config file at all — per-commodity data lives entirely in
the ledger, and anything that isn't per-commodity is passed explicitly
at call time rather than read from a file.

Source ticker mapping from the ledger itself, using the same convention
bean-price already uses:

```
2000-01-01 commodity AAPL
  price: "USD:yahoo/AAPL"
```

- Parse every `Commodity` directive's `price` metadata string as
  `CURRENCY:source/TICKER`. Take `TICKER` as the yfinance symbol and
  `CURRENCY` as the quote currency to write on the `Price` directive.
- Ignore/validate the `source` segment (`yahoo` here) — this library only
  ever calls yfinance, so `source` isn't used to select a fetcher, but
  warn if it's something unrecognized as a sanity check that you haven't
  copied metadata intended for a different bean-price source.
- If a commodity has no `price` metadata at all, default
  `ticker = commodity code`, `currency = ledger's operating currency`.
- **Extra per-commodity overrides**, as additional metadata keys on the
  same `Commodity` directive:
  ```
  2000-01-01 commodity GOOG
    price: "USD:yahoo/GOOG"
    price-frequency: "weekly-friday"
    price-start-date: 2020-01-01
  ```
  `price-frequency` overrides the default required-date frequency (see
  3.2); `price-start-date` overrides the auto-detected first-held date if
  you want to backfill further back than your first transaction.
- This logic is just a function or two inside `ledger.py` (it's reading
  metadata off directives `ledger.py` already parses) — there's no
  standalone `config.py` module.
- **Global defaults** (default frequency, thread count, retry count/
  backoff) are not read from any file. They're either:
  - constructor arguments on the main class (e.g.
    `PriceFetcher(default_frequency="weekly-friday", threads=6, retries=3)`), or
  - CLI flags with sane hardcoded defaults (`--threads`, `--retries`,
    `--default-frequency`) that override the constructor defaults when
    running via `cli.py`.
  Pick one call pattern and use it consistently — e.g. `cli.py` builds
  the options object from argparse and passes it into the same class
  a library caller would instantiate directly, so there's exactly one
  code path for "what are the effective settings," not two that can
  drift apart.

### 4.4 `fetcher.py` — Threaded yfinance fetching

- One `yfinance.Ticker(ticker).history(start=, end=)` call per
  commodity (not per date) — this is the main API-efficiency lever.
- Use `concurrent.futures.ThreadPoolExecutor` to fetch multiple tickers
  in parallel. This is I/O-bound (network), so threading is appropriate
  (no need for multiprocessing).
- Configurable `--threads N` (default something conservative, e.g. 4-6,
  since Yahoo will throttle/block aggressive parallel scraping).
- Per-ticker retry with exponential backoff on failure (yfinance/Yahoo
  endpoints are unofficial and flaky — expect occasional empty responses
  or rate-limit errors).
- Failures for one ticker must not abort the whole batch — collect
  per-ticker success/failure and report a summary at the end.
- Filter fetched rows down to just the originally-missing dates (yfinance
  returns all trading days in the range; only keep the ones you asked for
  to avoid accidentally re-deriving dates you already have from another
  source).

### 4.5 `writer.py` — Writing Price directives

- For each commodity, append new `Price` directives to
  `prices/<COMMODITY>.beancount`, in beancount syntax:
  ```
  2024-03-15 price SPY 512.34 USD
  ```
- Append-and-sort: append new lines, then re-sort the file by date for
  human readability (beancount itself doesn't care about order, but you
  will when eyeballing/diffing).
- Deduplicate: never write a `(date, commodity)` pair that's already in
  the file — check both freshly-loaded ledger state and any prices
  written earlier in the same run (in case a commodity is priced in
  multiple currencies).
- Create the file with a header comment if it doesn't exist yet.

### 4.6 `cli.py` — CLI

Three commands:

- `list-missing [--commodity SPY] [--since DATE]` — print a table of
  commodities and missing date counts/ranges, no network calls. Good for
  a quick sanity check before fetching.
- `fetch [--dry-run] [--threads N] [--commodity SPY] [--since DATE]` —
  run the full pipeline. `--dry-run` fetches from yfinance and shows what
  *would* be written, without touching files.
- Exit non-zero if any ticker failed, so this is safe to run in cron/CI.

### 4.7 Testing — write tests first, against a shared example ledger

Build this test-first: write the fixture ledger and the tests for a
component before writing that component's implementation. This matters
more than usual here because most of the logic (held-date ranges,
missing-date math, dedup) is easy to get subtly wrong and hard to verify
by eye.

- **Example ledger fixture**: create one `tests/fixtures/example.beancount`
  used across all test modules — not a different ad-hoc snippet per test.
  It should include, at minimum:
  - A handful of commodities with `Commodity` directives using the
    `price: "USD:yahoo/TICKER"` metadata convention (section 3.3),
    including at least one with `price-frequency` and `price-start-date`
    overrides.
  - At least one commodity with no `Commodity` directive at all, to
    exercise the fallback path.
  - Overlapping and non-overlapping holding periods across commodities
    (one bought/sold and rebought, one held continuously, one closed
    out entirely) to exercise held-range detection.
  - Some existing `Price` directives already present for a few
    commodities/dates, deliberately leaving known gaps, so expected
    "missing" output is hand-computable and checked into the test itself
    as the expected result.
  - At least one duplicate `(date, commodity)` `Price` directive
    scenario for the migration/dedup logic.
- Keep this fixture under version control and treat changes to it as
  changes to the test suite — updating the fixture without updating the
  corresponding expected-output assertions should fail tests loudly.
- **Order per component**: for each of `ledger.py` (including metadata/
  ticker parsing), `requirements.py`, `writer.py`, and `migrate.py`,
  write the tests (using the shared fixture, or targeted temp-file
  fixtures for `writer.py`/`migrate.py` where file I/O is being tested)
  before the implementation. For `fetcher.py`, write tests against a
  mocked `yfinance.Ticker.history` first, since no real network fixture
  is possible/desirable in CI.
- This means the build order in section 4 below should read as
  "tests, then implementation" for each numbered step, not tests as a
  final pass at the end.

### 4.8 `migrate.py` — One-time refactor: dated files → per-symbol files

A separate, one-time CLI command to convert your existing
`prices/YYYY-MM-DD.beancount` layout into the per-symbol layout this
library expects. Run once, then retired (but keep the code — useful if
you ever import a ledger from someone else using the old layout).

- **Input**: the `prices/` directory of dated files (glob
  `prices/*.beancount` matching a date-like filename).
- **Parse**: use `beancount.parser.parser.parse_file()` (or
  `loader.load_file` against just that directory's files, bypassing
  main-ledger validation) to extract every `Price` directive from every
  dated file, in memory, as `(date, commodity, amount, currency)` tuples.
- **Group**: bucket all parsed `Price` entries by commodity.
- **Deduplicate**: if the same `(date, commodity)` appears more than once
  (shouldn't happen, but verify), keep the first and log a warning rather
  than silently dropping — a duplicate is a sign something upstream is
  wrong and worth knowing about.
- **Write**: for each commodity, write `prices/<COMMODITY>.beancount`
  with all its `Price` directives, sorted by date — reusing `writer.py`'s
  write/sort/dedup logic rather than duplicating it.
- **Verify before deleting anything**:
  - Count total `Price` directives read vs. total written across all new
    per-symbol files — must match exactly.
  - Reload the *new* per-symbol files with `beancount.loader.load_file`
    and confirm zero parse errors.
  - Optionally, load both the old and new layouts independently and diff
    the resulting `beancount.core.prices.build_price_map()` output to
    confirm they produce an identical price map — this is the strongest
    guarantee that the refactor is lossless.
- **Move, don't delete**: on success, move the old dated files to
  `prices/_archive_dated/` (or similar) rather than deleting them
  outright. Deleting is a one-way door; moving costs nothing and gives
  you a rollback for free. Only remove the archive directory yourself,
  later, once you've lived with the new layout for a while.
- **CLI**: `migrate-dated-prices [--dry-run] [--prices-dir prices/]` —
  dry-run prints the plan (N dated files → M per-symbol files, X total
  directives) without writing or moving anything.

This depends on `writer.py` from section 3.5, so build it after that
component, not before.

## 5. Build order (for the coding agent)

0. Build `tests/fixtures/example.beancount` (section 3.7) first, before
   any implementation code. Write it, read it back over, and confirm by
   hand what the expected held-ranges/missing-dates/duplicates should be
   for it — those hand-computed expectations become the assertions in
   step 1's tests.
1. Write tests for `models.py` + `ledger.py` against the fixture (held-
   range extraction, existing-price extraction, and `Commodity` metadata/
   ticker parsing) — they will fail (no implementation yet). Then
   implement `ledger.py` until they pass.
2. Write tests for `requirements.py` using fake held-ranges/existing-
   price sets (no beancount parsing needed here, so these can be
   hand-built rather than pulled from the fixture) — then implement.
3. Write tests for `writer.py` against temp files (append/dedup/sort) —
   then implement.
4. Write tests for `migrate.py` against a small set of dated fixture
   files with a deliberate duplicate — then implement, reusing
   `writer.py`. Run it against a copy of your real `prices/` directory
   before relying on the new layout for anything.
5. Write tests for `fetcher.py` against a mocked `yfinance.Ticker.history`
   — then implement single-threaded, then add threading once the
   single-threaded path passes.
6. `cli.py` — wire it all together (`list-missing`, `fetch`,
   `migrate-dated-prices`); a thin layer, so a few integration-style
   tests here are enough rather than full tests-first treatment.
7. End-to-end smoke test running the whole pipeline against the shared
   fixture ledger, network mocked.

## 6. Open decisions to confirm before/during implementation

- Default required-date frequency: daily vs. weekly-Friday as the global
  default (recommend weekly-Friday as default, daily as opt-in per
  commodity — much less API load, still fine for most reporting/returns
  use cases).
- Whether "held" should count commodities held at cost only, or also
  ones just referenced in `Commodity` directives regardless of holdings.
- Threshold for retry/backoff (how many retries, how long) — yfinance
  reliability varies day to day.