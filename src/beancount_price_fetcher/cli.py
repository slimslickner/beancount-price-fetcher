"""beancount-price-fetcher CLI: list-missing, fetch, migrate-dated-prices.

Thin layer over the library code. CLI is the only place that configures
logging (so library imports don't hijack a caller's logging).
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from pathlib import Path

import click

from . import __version__
from .constants import DEFAULT_FREQUENCY, DEFAULT_RETRY_COUNT, DEFAULT_THREAD_COUNT
from .fetcher import PriceFetcher
from .ledger import analyze_ledger
from .migrate import migrate_dated_prices
from .models import FetchedPrice, Frequency
from .requirements import compute_requirements
from .writer import DEFAULT_FILE_EXTENSION, PriceWriter


@click.group()
@click.version_option(__version__, prog_name="beanprices")
@click.option("-v", "--verbose", count=True, help="Increase verbosity (-v, -vv).")
def cli(verbose: int) -> None:
    """Scan a Beancount ledger for missing commodity prices and backfill via yfinance."""
    level = logging.WARNING - 10 * verbose
    logging.basicConfig(
        level=max(level, logging.DEBUG),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@cli.command("list-missing")
@click.option(
    "--ledger",
    required=True,
    type=click.Path(exists=True),
    help="Path to the main Beancount ledger file.",
)
@click.option("--commodity", default=None, help="Filter to a single commodity code (e.g. SPY).")
@click.option(
    "--since",
    default=None,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Only show commodities with missing dates >= this date.",
)
@click.option(
    "--default-frequency",
    default=DEFAULT_FREQUENCY.value,
    type=click.Choice([f.value for f in Frequency]),
    help=f"Default required-date frequency (default: {DEFAULT_FREQUENCY.value}).",
)
def list_missing(
    ledger: str,
    commodity: str | None,
    since: datetime | None,
    default_frequency: str,
) -> None:
    """List commodities with missing prices (no network calls)."""
    analysis = analyze_ledger(ledger)
    since_date: date | None = since.date() if since is not None else None
    reqs = compute_requirements(
        analysis.held_periods,
        analysis.existing_prices,
        analysis.metadata,
        default_frequency=Frequency(default_frequency),
    )
    click.echo(f"Ledger: {ledger}")
    click.echo(f"Operating currencies: {', '.join(sorted(analysis.operating_currencies))}")
    click.echo(f"Today: {analysis.today.isoformat()}")
    click.echo("")
    click.echo(f"{'commodity':12} {'missing':>8}  {'min_date':12} {'max_date':12} {'ticker':12}")
    for req in sorted(reqs, key=lambda r: r.commodity):
        if commodity is not None and req.commodity != commodity:
            continue
        if since_date is not None and req.max_date < since_date:
            continue
        click.echo(
            f"{req.commodity:12} {len(req.missing_dates):>8}  "
            f"{req.min_date.isoformat():12} {req.max_date.isoformat():12} "
            f"{req.ticker:12}"
        )


@cli.command()
@click.option(
    "--ledger",
    required=True,
    type=click.Path(exists=True),
    help="Path to the main Beancount ledger file.",
)
@click.option(
    "--prices-dir",
    default="prices",
    type=click.Path(),
    help="Directory for per-symbol price files (default: prices).",
)
@click.option(
    "--file-extension",
    default=DEFAULT_FILE_EXTENSION,
    show_default=True,
    help="Extension for per-symbol price files (default: .bean).",
)
@click.option("--dry-run", is_flag=True, help="Compute what would happen; don't write or fetch.")
@click.option(
    "--threads",
    default=DEFAULT_THREAD_COUNT,
    type=int,
    help=f"Thread pool size (default: {DEFAULT_THREAD_COUNT}).",
)
@click.option(
    "--retries",
    default=DEFAULT_RETRY_COUNT,
    type=int,
    help=f"Per-ticker retry attempts (default: {DEFAULT_RETRY_COUNT}).",
)
@click.option(
    "--default-frequency",
    default=DEFAULT_FREQUENCY.value,
    type=click.Choice([f.value for f in Frequency]),
    help=f"Default required-date frequency (default: {DEFAULT_FREQUENCY.value}).",
)
@click.option("--commodity", default=None, help="Only fetch for one commodity.")
@click.option(
    "--since",
    default=None,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Only consider missing dates >= this date.",
)
def fetch(
    ledger: str,
    prices_dir: str,
    file_extension: str,
    dry_run: bool,
    threads: int,
    retries: int,
    default_frequency: str,
    commodity: str | None,
    since: datetime | None,
) -> None:
    """Run the full pipeline: analyze -> fetch -> write."""
    analysis = analyze_ledger(ledger)
    reqs = compute_requirements(
        analysis.held_periods,
        analysis.existing_prices,
        analysis.metadata,
        default_frequency=Frequency(default_frequency),
    )
    if commodity is not None:
        reqs = [r for r in reqs if r.commodity == commodity]
    if since is not None:
        since_date: date = since.date()
        reqs = [r for r in reqs if r.max_date >= since_date]
    click.echo(f"Found {len(reqs)} commodity(ies) needing prices.")
    if dry_run:
        click.echo("Dry run: skipping fetch.")
        for r in sorted(reqs, key=lambda x: x.commodity):
            click.echo(
                f"  {r.commodity}: {len(r.missing_dates)} dates, "
                f"{r.min_date} -> {r.max_date} via {r.ticker}"
            )
        return

    fetcher = PriceFetcher(threads=threads, retries=retries)
    successes, failures = fetcher.fetch_all(reqs, dry_run=False)
    click.echo(f"Fetched {len(successes)} prices.")
    if failures:
        click.echo(f"Failed: {len(failures)} ticker(s):", err=True)
        for req, exc in failures:
            click.echo(f"  {req.commodity} ({req.ticker}): {exc}", err=True)

    writer = PriceWriter(
        prices_dir=Path(prices_dir),
        file_extension=file_extension,
        display_precision=analysis.display_precision,
    )
    by_commodity: dict[str, list[FetchedPrice]] = {}
    for fp in successes:
        by_commodity.setdefault(fp.commodity, []).append(fp)
    total_written = 0
    for c, fp_list in by_commodity.items():
        total_written += writer.write_commodity(c, fp_list)
    click.echo(f"Wrote {total_written} prices to {prices_dir}/")

    if failures:
        sys.exit(1)


@cli.command("migrate-dated-prices")
@click.option(
    "--prices-dir",
    default="prices",
    type=click.Path(exists=True),
    help="Directory of dated price files (default: prices).",
)
@click.option("--dry-run", is_flag=True, help="Print the plan without moving or writing.")
def migrate_cmd(prices_dir: str, dry_run: bool) -> None:
    """One-time: convert bean-price dated files (.bean / .gen.bean) -> per-symbol files."""
    result = migrate_dated_prices(prices_dir=Path(prices_dir), dry_run=dry_run)
    click.echo(
        f"{'[DRY RUN] ' if dry_run else ''}"
        f"Dated files: {result.dated_files_count}  "
        f"Per-symbol files: {result.per_symbol_files_count}  "
        f"Total prices: {result.total_prices}  "
        f"Duplicates warned: {result.duplicates_warned}"
    )
    if dry_run:
        click.echo("Originals would be moved to _archive_dated/; nothing was touched.")


def main() -> None:
    """Console-script entry point registered in pyproject.toml."""
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
