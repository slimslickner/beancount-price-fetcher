"""beancount-price-fetcher CLI: list-missing, fetch, fetch-metadata, migrate-dated-prices.

Thin layer over the library code. CLI is the only place that configures
logging (so library imports don't hijack a caller's logging).
"""

from __future__ import annotations

import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import click

from . import __version__
from .constants import (
    DEFAULT_FREQUENCY,
    DEFAULT_RETRY_COUNT,
    DEFAULT_THREAD_COUNT,
    METADATA_KEYS,
)
from .fetcher import PriceFetcher
from .ledger import CommodityDirective, LedgerAnalysis, analyze_ledger
from .metadata_fetcher import (
    MetadataFetcher,
    MetadataLookupRequest,
    plan_metadata_changes,
)
from .metadata_writer import (
    DirectiveEdit,
    NewMetadataDirective,
    append_new_directives,
    apply_directive_edits,
)
from .migrate import migrate_dated_prices
from .models import (
    CommodityInfo,
    FetchedPrice,
    Frequency,
    MetadataPlan,
    MetadataStatus,
)
from .requirements import compute_requirements
from .writer import DEFAULT_FILE_EXTENSION, PriceWriter

logger = logging.getLogger(__name__)

_INCLUDE_RE = re.compile(r'^\s*include\s+"([^"]+)"', re.MULTILINE)
_ERROR_DETAIL_LIMIT = 80


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
        skipped_commodities=analysis.skipped_commodities,
    )
    click.echo(f"Ledger: {ledger}")
    click.echo(f"Operating currencies: {', '.join(sorted(analysis.operating_currencies))}")
    click.echo(f"Today: {analysis.today.isoformat()}")
    if analysis.skipped_commodities:
        click.echo(
            f"Skipped (no `price` metadata): {', '.join(sorted(analysis.skipped_commodities))}"
        )
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
@click.option(
    "--end-date",
    default=None,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Inclusive upper bound on the fetched range (default: today).",
)
@click.option(
    "--include-today",
    is_flag=True,
    default=False,
    help="Include today's price even if the market is still open (intraday snapshot).",
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
    end_date: datetime | None,
    include_today: bool,
) -> None:
    """Run the full pipeline: analyze -> fetch -> write."""
    analysis = analyze_ledger(ledger)
    reqs = compute_requirements(
        analysis.held_periods,
        analysis.existing_prices,
        analysis.metadata,
        default_frequency=Frequency(default_frequency),
        skipped_commodities=analysis.skipped_commodities,
    )
    if commodity is not None:
        reqs = [r for r in reqs if r.commodity == commodity]
    if since is not None:
        since_date: date = since.date()
        reqs = [r for r in reqs if r.max_date >= since_date]
    click.echo(f"Found {len(reqs)} commodity(ies) needing prices.")
    if analysis.skipped_commodities:
        click.echo(
            f"Skipped (no `price` metadata): {', '.join(sorted(analysis.skipped_commodities))}"
        )
    if dry_run:
        click.echo("Dry run: skipping fetch.")
        for r in sorted(reqs, key=lambda x: x.commodity):
            click.echo(
                f"  {r.commodity}: {len(r.missing_dates)} dates, "
                f"{r.min_date} -> {r.max_date} via {r.ticker}"
            )
        return

    today_arg: date | None = None if include_today else date.today()
    if include_today:
        logger.warning(
            "--include-today: today's price may be an intraday snapshot if the market is still open"
        )
    end_date_arg: date | None = end_date.date() if end_date is not None else None

    fetcher = PriceFetcher(threads=threads, retries=retries)
    successes, failures = fetcher.fetch_all(
        reqs, dry_run=False, today=today_arg, end_date=end_date_arg
    )
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


@cli.command("fetch-metadata")
@click.option(
    "--ledger",
    required=True,
    type=click.Path(exists=True),
    help="Path to the main Beancount ledger file.",
)
@click.option("--commodity", default=None, help="Only process one commodity code.")
@click.option(
    "--all",
    "include_all",
    is_flag=True,
    default=False,
    help="Include every commodity with a `commodity` directive, not just held ones.",
)
@click.option(
    "--keys",
    default=None,
    help="Comma-separated metadata keys to add/update (default: all).",
)
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Also update existing values that differ from the provider.",
)
@click.option(
    "--write",
    "do_write",
    is_flag=True,
    default=False,
    help="Apply the edits (default is a preview diff).",
)
@click.option(
    "--output-file",
    default=None,
    type=click.Path(),
    help="File for new `commodity` directives (default: commodities.bean next to the ledger).",
)
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
def fetch_metadata(
    ledger: str,
    commodity: str | None,
    include_all: bool,
    keys: str | None,
    refresh: bool,
    do_write: bool,
    output_file: str | None,
    threads: int,
    retries: int,
) -> None:
    """Look up descriptive commodity metadata via yfinance and write it to the ledger."""
    selected_keys = _parse_metadata_keys(keys)
    analysis = analyze_ledger(ledger)
    directives = analysis.commodity_directives
    held = analysis.held_periods

    scope = _metadata_scope(analysis, commodity, include_all)

    rows: list[tuple[str, MetadataStatus, str]] = []
    requests: list[MetadataLookupRequest] = []
    pending: dict[str, tuple[CommodityDirective | None, dict[str, object]]] = {}
    for code in sorted(scope):
        directive = directives.get(code)
        existing: dict[str, object] = directive.metadata if directive is not None else {}
        if not refresh and all(key in existing for key in selected_keys):
            rows.append((code, MetadataStatus.COMPLETE, ""))
            continue
        requests.append(
            MetadataLookupRequest(
                code,
                _resolve_ticker(code, analysis),
                fetch_isin="yf_isin" in selected_keys,
            )
        )
        pending[code] = (directive, existing)

    outcomes = _lookup_metadata(requests, threads, retries)

    edits_by_file: dict[Path, list[DirectiveEdit]] = defaultdict(list)
    new_directives: list[NewMetadataDirective] = []
    errors: list[str] = []

    for code in sorted(pending):
        directive, existing = pending[code]
        outcome = outcomes[code]
        if isinstance(outcome, Exception):
            rows.append((code, MetadataStatus.ERROR, _short_error(outcome)))
            errors.append(f"{code}: {outcome}")
            continue
        plan = plan_metadata_changes(code, existing, outcome, selected_keys, refresh=refresh)
        if directive is None and plan.adds and code not in held:
            rows.append((code, MetadataStatus.ERROR, "no held period to date a new directive"))
            errors.append(f"{code}: no held period to date a new directive")
            continue
        rows.append((code, plan.status, _plan_detail(plan)))
        if directive is None:
            if plan.adds:
                first = min(period.first for period in held[code])
                new_directives.append(NewMetadataDirective(code, first, plan.adds))
        elif plan.adds or plan.updates:
            edits_by_file[Path(directive.filename)].append(
                DirectiveEdit(
                    commodity=code,
                    lineno=directive.lineno,
                    adds=plan.adds,
                    updates=tuple((key, new) for key, _old, new in plan.updates),
                )
            )

    click.echo(f"{'commodity':12} {'status':10} detail")
    for code, status, detail in sorted(rows, key=lambda row: row[0]):
        click.echo(f"{code:12} {status.value:10} {detail}")

    file_edits = [(path, edits_by_file[path]) for path in sorted(edits_by_file)]
    previews = [
        (path, edits, apply_directive_edits(path, edits, write=False)) for path, edits in file_edits
    ]

    output_path = (
        Path(output_file) if output_file is not None else Path(ledger).parent / "commodities.bean"
    )
    output_preview = (
        append_new_directives(output_path, new_directives, write=False) if new_directives else None
    )

    blocking_errors = [error for _path, _edits, result in previews for error in result.errors]
    if output_preview is not None:
        blocking_errors.extend(output_preview.errors)

    if blocking_errors:
        for error in blocking_errors:
            click.echo(error, err=True)
            errors.append(error)
    else:
        if do_write:
            for path, edits in file_edits:
                apply_directive_edits(path, edits, write=True)
            if output_preview is not None:
                append_new_directives(output_path, new_directives, write=True)
        for _path, _edits, result in previews:
            if result.diff:
                click.echo(result.diff)
        if output_preview is not None:
            if output_preview.diff:
                click.echo(output_preview.diff)
            if do_write and not _is_included(Path(ledger), output_path.name):
                click.echo(f'Reminder: add `include "{output_path.name}"` to {ledger}')

    if not do_write:
        click.echo("Preview only; rerun with --write to apply.")
    if errors:
        sys.exit(1)


def _lookup_metadata(
    requests: list[MetadataLookupRequest], threads: int, retries: int
) -> dict[str, CommodityInfo | Exception]:
    """Run metadata lookups, showing a live progress bar on an interactive stderr.

    yfinance's own logger is muted for the duration: it writes ERROR records
    (e.g. an HTTP 404 for an unknown symbol) that are redundant with this
    command's status table and collide with the bar's ``\r`` redraws.
    """
    fetcher = MetadataFetcher(threads=threads, retries=retries)
    if not requests:
        return {}
    yfinance_logger = logging.getLogger("yfinance")
    previous_level = yfinance_logger.level
    yfinance_logger.setLevel(logging.CRITICAL)
    try:
        if not sys.stderr.isatty():
            click.echo(f"Looking up metadata for {len(requests)} commodit(ies)...", err=True)
            return fetcher.fetch_all(requests)
        with click.progressbar(
            length=len(requests), label="Looking up metadata", file=sys.stderr
        ) as bar:
            return fetcher.fetch_all(requests, on_result=lambda _code: bar.update(1))
    finally:
        yfinance_logger.setLevel(previous_level)


def _short_error(exc: Exception) -> str:
    """Collapse an exception message to one bounded line for the status table."""
    text = " ".join(str(exc).split())
    if len(text) <= _ERROR_DETAIL_LIMIT:
        return text
    return text[: _ERROR_DETAIL_LIMIT - 1].rstrip() + "\u2026"


def _parse_metadata_keys(keys: str | None) -> list[str]:
    """Parse/validate the ``--keys`` option; None means all known keys."""
    if keys is None:
        return list(METADATA_KEYS)
    parsed = [key.strip() for key in keys.split(",") if key.strip()]
    if not parsed:
        raise click.BadParameter("--keys must name at least one key")
    unknown = [key for key in parsed if key not in METADATA_KEYS]
    if unknown:
        raise click.BadParameter(
            f"unknown key(s): {', '.join(unknown)}; expected one of: {', '.join(METADATA_KEYS)}"
        )
    return parsed


def _metadata_scope(analysis: LedgerAnalysis, commodity: str | None, include_all: bool) -> set[str]:
    """Determine which commodities the metadata command should consider."""
    if commodity is not None:
        if commodity in analysis.operating_currencies:
            msg = f"commodity {commodity} is an operating currency; nothing to fetch"
            raise click.BadParameter(msg)
        if (
            commodity not in analysis.held_periods
            and commodity not in analysis.commodity_directives
        ):
            msg = f"commodity {commodity} is neither held nor declared in the ledger"
            raise click.BadParameter(msg)
        return {commodity}
    if include_all:
        scope = set(analysis.held_periods) | set(analysis.commodity_directives)
    else:
        scope = set(analysis.held_periods)
    return scope - analysis.operating_currencies


def _resolve_ticker(commodity: str, analysis: LedgerAnalysis) -> str:
    """Ticker from the commodity's ``price`` metadata, falling back to the code."""
    metadata = analysis.metadata.get(commodity)
    return metadata.ticker if metadata is not None else commodity


def _display_metadata_value(value: object) -> str:
    """Render a stored or provider value for the status detail column."""
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def _plan_detail(plan: MetadataPlan) -> str:
    """Human-readable detail column for a metadata plan."""
    if plan.status is MetadataStatus.FILLED:
        return "added " + ", ".join(key for key, _value in plan.adds)
    if plan.status is MetadataStatus.UPDATED:
        return "updated " + ", ".join(
            f"{key} ({_display_metadata_value(old)} -> {_display_metadata_value(new)})"
            for key, old, new in plan.updates
        )
    if plan.status is MetadataStatus.PARTIAL:
        return "some selected keys unavailable from provider"
    if plan.status is MetadataStatus.NOT_FOUND:
        return "provider returned no usable data"
    return ""


def _is_included(ledger_path: Path, output_name: str) -> bool:
    """True if ``output_name`` is reachable from ``ledger_path`` via includes.

    Walks the whole ``include`` graph (relative to each including file), so a
    file included from a sub-file is recognised, not just direct includes in
    the main ledger.
    """
    seen: set[Path] = set()
    queue = [ledger_path]
    while queue:
        current = queue.pop()
        resolved = current.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            content = current.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _INCLUDE_RE.finditer(content):
            target = match.group(1)
            if Path(target).name == output_name:
                return True
            queue.append(current.parent / target)
    return False


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
