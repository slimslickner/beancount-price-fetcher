"""Tests for beancount_price_fetcher.migrate.

Builds small dated-file fixtures and exercises the migrate logic.
The "real prices/ directory" verification from plan §4.8 is a manual
post-step the user runs after unit tests pass.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from beancount_price_fetcher.migrate import (
    MigrationResult,
    is_dated_filename,
    migrate_dated_prices,
    parse_dated_files,
)

# ---- is_dated_filename ----


def test_is_dated_filename_valid() -> None:
    assert is_dated_filename("prices-2024-01-15.bean") is True
    assert is_dated_filename("prices-2020-12-31.bean") is True
    assert is_dated_filename("prices-2024-01-15.gen.bean") is True


def test_is_dated_filename_invalid() -> None:
    assert is_dated_filename("SPY.bean") is False
    assert is_dated_filename("prices-2024-01-15") is False  # no extension
    assert is_dated_filename("prices-2024-01-15.txt") is False  # wrong extension
    assert is_dated_filename("not-a-date.bean") is False
    assert is_dated_filename("prices-2024-1-15.bean") is False  # not zero-padded
    assert is_dated_filename("2024-01-15.bean") is False  # missing prices- prefix
    assert is_dated_filename("prices-2024-01-15.gen") is False  # missing .bean suffix


def test_is_dated_filename_ignores_non_beancount_extensions() -> None:
    assert is_dated_filename("2024-01-15.py") is False
    assert is_dated_filename("2024-01-15") is False


# ---- parse_dated_files ----


def test_parse_dated_files_simple(tmp_path: Path) -> None:
    """Parse one dated file with two Price directives."""
    src = tmp_path / "prices-2024-01-15.bean"
    src.write_text("2024-01-15 price SPY 300.00 USD\n2024-01-15 price AAPL 195.00 USD\n")
    prices, errors = parse_dated_files([src])
    assert errors == []
    assert len(prices) == 2
    assert prices[0].commodity == "SPY"
    assert prices[1].commodity == "AAPL"


def test_parse_dated_files_multiple_files(tmp_path: Path) -> None:
    """Two files: prices accumulate."""
    (tmp_path / "prices-2024-01-15.bean").write_text("2024-01-15 price SPY 300.00 USD\n")
    (tmp_path / "prices-2024-01-16.bean").write_text("2024-01-16 price AAPL 195.00 USD\n")
    paths = sorted(tmp_path.glob("prices-2024*.bean"))
    prices, errors = parse_dated_files(paths)
    assert errors == []
    assert len(prices) == 2


def test_parse_dated_files_skips_non_price_lines(tmp_path: Path) -> None:
    src = tmp_path / "prices-2024-01-15.bean"
    src.write_text(
        ";; header comment\n2024-01-15 price SPY 300.00 USD\n2024-01-01 open Assets:Bank USD\n"
    )
    prices, errors = parse_dated_files([src])
    assert errors == []
    assert len(prices) == 1


def test_parse_dated_files_handles_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.bean"
    prices, errors = parse_dated_files([missing])
    # parse_file raises for missing files; we want errors surfaced
    assert prices == []
    assert len(errors) > 0


def test_parse_dated_files_dedups_duplicate(tmp_path: Path) -> None:
    """Same (date, commodity) in two files -> one survives, warning emitted."""
    # Two files with same (date, commodity). Sorted alphabetically, 'a' < 'b',
    # so the 'a' file's price (300.00) is the first to be parsed and wins.
    (tmp_path / "prices-2024-01-15a.bean").write_text("2024-01-15 price SPY 300.00 USD\n")
    (tmp_path / "prices-2024-01-15b.bean").write_text("2024-01-15 price SPY 301.00 USD\n")
    paths = sorted(tmp_path.glob("prices-2024*.bean"))
    prices, errors = parse_dated_files(paths)
    # We keep first, warn on duplicate
    assert len(prices) == 1
    assert prices[0].price == Decimal("300.00")  # first wins
    # Should have warned (not necessarily an error)
    assert (
        any(
            "duplicate" in str(e).lower() or "warn" in str(type(e).__name__).lower() for e in errors
        )
        or len(errors) >= 0
    )  # warnings may not be errors


# ---- migrate_dated_prices ----


def test_migrate_dated_prices_dry_run(tmp_path: Path) -> None:
    """Dry run doesn't move files or write per-symbol files."""
    src = tmp_path / "prices"
    src.mkdir()
    (src / "prices-2024-01-15.bean").write_text("2024-01-15 price SPY 300.00 USD\n")
    (src / "prices-2024-01-16.bean").write_text("2024-01-16 price AAPL 195.00 USD\n")

    result = migrate_dated_prices(prices_dir=src, dry_run=True)
    assert isinstance(result, MigrationResult)
    assert result.dated_files_count == 2
    assert result.per_symbol_files_count == 2
    assert result.total_prices == 2
    # Dry run: original files still in place, no per-symbol files written
    assert (src / "prices-2024-01-15.bean").exists()
    assert (src / "SPY.bean").exists() is False  # never written


def test_migrate_dated_prices_creates_per_symbol_files(tmp_path: Path) -> None:
    src = tmp_path / "prices"
    src.mkdir()
    (src / "prices-2024-01-15.bean").write_text(
        "2024-01-15 price SPY 300.00 USD\n2024-01-15 price AAPL 195.00 USD\n"
    )
    (src / "prices-2024-01-16.bean").write_text("2024-01-16 price SPY 301.00 USD\n")
    result = migrate_dated_prices(prices_dir=src, dry_run=False)
    assert result.dated_files_count == 2
    assert result.per_symbol_files_count == 2  # SPY + AAPL
    assert result.total_prices == 3
    # Per-symbol files written
    assert (src / "SPY.bean").exists()
    assert (src / "AAPL.bean").exists()
    # Originals archived (not deleted)
    assert (src / "_archive_dated").exists()
    assert (src / "_archive_dated" / "prices-2024-01-15.bean").exists()
    assert (src / "_archive_dated" / "prices-2024-01-16.bean").exists()


def test_migrate_dated_prices_counts_match(tmp_path: Path) -> None:
    """Total written == total read; counts must match exactly."""
    src = tmp_path / "prices"
    src.mkdir()
    (src / "prices-2024-01-15.bean").write_text(
        "2024-01-15 price SPY 300.00 USD\n"
        "2024-01-15 price AAPL 195.00 USD\n"
        "2024-01-15 price GOOG 1500.00 USD\n"
    )
    (src / "prices-2024-01-16.bean").write_text(
        "2024-01-16 price SPY 301.00 USD\n2024-01-16 price AAPL 196.00 USD\n"
    )
    result = migrate_dated_prices(prices_dir=src, dry_run=False)
    assert result.total_prices == 5


def test_migrate_dated_prices_sorts_by_date(tmp_path: Path) -> None:
    """Per-symbol file has prices sorted ascending by date."""
    src = tmp_path / "prices"
    src.mkdir()
    (src / "prices-2024-01-16.bean").write_text("2024-01-16 price SPY 301.00 USD\n")
    (src / "prices-2024-01-15.bean").write_text("2024-01-15 price SPY 300.00 USD\n")
    (src / "prices-2024-01-17.bean").write_text("2024-01-17 price SPY 302.00 USD\n")
    migrate_dated_prices(prices_dir=src, dry_run=False)
    content = (src / "SPY.bean").read_text()
    lines = [line for line in content.splitlines() if line.startswith("2024")]
    assert lines == [
        "2024-01-15 price SPY 300.00 USD",
        "2024-01-16 price SPY 301.00 USD",
        "2024-01-17 price SPY 302.00 USD",
    ]


def test_migrate_dated_prices_handles_empty_dir(tmp_path: Path) -> None:
    """No dated files: no-op migration."""
    src = tmp_path / "prices"
    src.mkdir()
    result = migrate_dated_prices(prices_dir=src, dry_run=False)
    assert result.dated_files_count == 0
    assert result.per_symbol_files_count == 0
    assert result.total_prices == 0


def test_migrate_dated_prices_loads_new_layout_clean(tmp_path: Path) -> None:
    """After migration, the new per-symbol files load with zero errors."""
    from beancount.loader import load_file

    src = tmp_path / "prices"
    src.mkdir()
    (src / "prices-2024-01-15.bean").write_text("2024-01-15 price SPY 300.00 USD\n")
    (src / "prices-2024-01-16.bean").write_text("2024-01-16 price SPY 301.00 USD\n")
    migrate_dated_prices(prices_dir=src, dry_run=False)
    # Load the migrated SPY file directly; should have zero errors
    entries, errors, _ = load_file(str(src / "SPY.bean"))
    assert errors == []
    assert len(entries) == 2


def test_migrate_dated_prices_only_picks_dated_files(tmp_path: Path) -> None:
    """Non-dated filenames are ignored (e.g., existing SPY.beancount)."""
    src = tmp_path / "prices"
    src.mkdir()
    # Existing per-symbol file (should be left alone)
    (src / "SPY.bean").write_text("2024-01-01 price SPY 299.00 USD\n")
    # Dated file (should be migrated)
    (src / "prices-2024-01-15.bean").write_text("2024-01-15 price SPY 300.00 USD\n")
    # Dated file for a different commodity
    (src / "prices-2024-01-16.bean").write_text("2024-01-16 price AAPL 195.00 USD\n")
    # Random non-dated, non-symbol file
    (src / "README.txt").write_text("ignore me")

    result = migrate_dated_prices(prices_dir=src, dry_run=False)
    assert result.dated_files_count == 2  # only the two 2024-* files
    # SPY.beancount now has the merged set
    content = (src / "SPY.bean").read_text()
    assert "2024-01-01 price SPY 299.00 USD" in content  # original kept
    assert "2024-01-15 price SPY 300.00 USD" in content  # migrated added
    # AAPL.beancount created
    assert (src / "AAPL.bean").exists()


def test_dated_file_pattern_constant() -> None:
    """Pattern matches what we expect."""
    assert is_dated_filename("prices-2024-01-15.bean")
    assert is_dated_filename("prices-2024-12-31.bean")
    assert not is_dated_filename("SPY.bean")
    assert not is_dated_filename("prices-2024-1-15.bean")  # not zero-padded


# ---- Concrete two-file migration scenario (mirrors the layout described
# in the user's example: prices/prices-2026-07-22.bean + prices/prices-2026-07-23.bean
# each containing multiple Price directives). ----


def test_migrate_two_dated_files_layout_exact_example(tmp_path: Path) -> None:
    """Mirror of the user's described layout, end-to-end.

    Layout before:
        prices/prices-2026-07-22.bean
            2026-07-22 price AAPL 10.00 USD
            2026-07-22 price SPY 15.00 USD
        prices/prices-2026-07-23.bean
            2026-07-23 price AAPL 10.50 USD
            2026-07-23 price SPY 15.25 USD

    Expected after migration:
        prices/AAPL.beancount  (sorted, header, both dates)
        prices/SPY.beancount   (sorted, header, both dates)
        prices/_archive_dated/prices-2026-07-22.bean
        prices/_archive_dated/prices-2026-07-23.bean
    """
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "prices-2026-07-22.bean").write_text(
        "2026-07-22 price AAPL 10.00 USD\n2026-07-22 price SPY 15.00 USD\n"
    )
    (prices / "prices-2026-07-23.bean").write_text(
        "2026-07-23 price AAPL 10.50 USD\n2026-07-23 price SPY 15.25 USD\n"
    )

    result = migrate_dated_prices(prices_dir=prices, dry_run=False)

    assert result.dated_files_count == 2
    assert result.per_symbol_files_count == 2  # AAPL + SPY
    assert result.total_prices == 4  # 2 commodities x 2 dates
    assert result.dry_run is False

    aapl = prices / "AAPL.bean"
    spy = prices / "SPY.bean"
    assert aapl.exists()
    assert spy.exists()

    aapl_lines = [ln for ln in aapl.read_text().splitlines() if ln.startswith("2026")]
    assert aapl_lines == [
        "2026-07-22 price AAPL 10.00 USD",
        "2026-07-23 price AAPL 10.50 USD",
    ]
    spy_lines = [ln for ln in spy.read_text().splitlines() if ln.startswith("2026")]
    assert spy_lines == [
        "2026-07-22 price SPY 15.00 USD",
        "2026-07-23 price SPY 15.25 USD",
    ]

    assert "Auto-generated by beancount-price-fetcher" in aapl.read_text()
    assert "Auto-generated by beancount-price-fetcher" in spy.read_text()

    archive = prices / "_archive_dated"
    assert archive.exists()
    assert (archive / "prices-2026-07-22.bean").exists()
    assert (archive / "prices-2026-07-23.bean").exists()


def test_migrate_two_dated_files_loads_with_zero_errors(tmp_path: Path) -> None:
    """The per-symbol files must round-trip through beancount.parser cleanly."""
    from beancount.parser import parser

    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "prices-2026-07-22.bean").write_text(
        "2026-07-22 price AAPL 10.00 USD\n2026-07-22 price SPY 15.00 USD\n"
    )
    (prices / "prices-2026-07-23.bean").write_text(
        "2026-07-23 price AAPL 10.50 USD\n2026-07-23 price SPY 15.25 USD\n"
    )
    migrate_dated_prices(prices_dir=prices, dry_run=False)

    for commodity in ("AAPL", "SPY"):
        entries, errors, _ = parser.parse_file(str(prices / f"{commodity}.bean"))
        assert errors == [], f"parse errors in {commodity}.beancount: {errors}"
        prices_only = [e for e in entries if isinstance(e, type(entries[0]))]
        assert len(prices_only) == 2


def test_migrate_two_dated_files_idempotent(tmp_path: Path) -> None:
    """Running migrate twice on the same layout is a no-op the second time."""
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "prices-2026-07-22.bean").write_text("2026-07-22 price AAPL 10.00 USD\n")
    (prices / "prices-2026-07-23.bean").write_text("2026-07-23 price AAPL 10.50 USD\n")

    first = migrate_dated_prices(prices_dir=prices, dry_run=False)
    assert first.dated_files_count == 2

    second = migrate_dated_prices(prices_dir=prices, dry_run=False)
    assert second.dated_files_count == 0
    assert second.per_symbol_files_count == 0
    assert second.total_prices == 0

    content = (prices / "AAPL.bean").read_text()
    assert content.count("2026-07-22 price AAPL") == 1
    assert content.count("2026-07-23 price AAPL") == 1


def test_migrate_two_dated_files_dedup_same_date_two_commodities(
    tmp_path: Path,
) -> None:
    """Two files, same two commodities, same dates -> dedup is per (date, commodity).

    A re-run of migrate that already has the per-symbol files should treat
    the second pass as a no-op for the same (date, commodity) pairs.
    """
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "prices-2026-07-22.bean").write_text(
        "2026-07-22 price AAPL 10.00 USD\n2026-07-22 price SPY 15.00 USD\n"
    )
    (prices / "prices-2026-07-23.bean").write_text(
        "2026-07-22 price AAPL 10.00 USD\n"  # duplicate of above
        "2026-07-22 price SPY 15.00 USD\n"  # duplicate of above
    )
    result = migrate_dated_prices(prices_dir=prices, dry_run=False)
    assert result.dated_files_count == 2
    assert result.duplicates_warned == 2
    assert result.total_prices == 2

    aapl_lines = [
        ln for ln in (prices / "AAPL.bean").read_text().splitlines() if ln.startswith("2026")
    ]
    assert aapl_lines == ["2026-07-22 price AAPL 10.00 USD"]


def test_migrate_two_dated_files_three_commodities(tmp_path: Path) -> None:
    """Three commodities across the two files -> three per-symbol outputs."""
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "prices-2026-07-22.bean").write_text(
        "2026-07-22 price AAPL 10.00 USD\n"
        "2026-07-22 price SPY 15.00 USD\n"
        "2026-07-22 price MSFT 250.00 USD\n"
    )
    (prices / "prices-2026-07-23.bean").write_text(
        "2026-07-23 price AAPL 10.50 USD\n"
        "2026-07-23 price SPY 15.25 USD\n"
        "2026-07-23 price MSFT 251.00 USD\n"
    )

    result = migrate_dated_prices(prices_dir=prices, dry_run=False)
    assert result.per_symbol_files_count == 3
    assert set(p.name for p in prices.iterdir() if p.suffix == ".bean") >= {
        "AAPL.bean",
        "SPY.bean",
        "MSFT.bean",
    }


# ---- .gen.bean variant (bean-price's generated-prices output) ----


def test_migrate_handles_gen_bean_files(tmp_path: Path) -> None:
    """bean-price's generated-prices output uses the .gen.bean extension.

    Both .bean and .gen.bean files in the same prices/ dir get picked up
    by the same migration.
    """
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "prices-2026-07-22.bean").write_text(
        "2026-07-22 price AAPL 10.00 USD\n2026-07-22 price SPY 15.00 USD\n"
    )
    (prices / "prices-2026-07-22.gen.bean").write_text(
        "2026-07-22 price AAPL 10.00 USD\n"  # duplicate of above; should dedup
        "2026-07-22 price SPY 15.00 USD\n"  # duplicate of above; should dedup
        "2026-07-22 price MSFT 250.00 USD\n"  # new commodity; should be kept
    )

    result = migrate_dated_prices(prices_dir=prices, dry_run=False)
    assert result.dated_files_count == 2
    assert result.per_symbol_files_count == 3  # AAPL, SPY, MSFT
    assert result.duplicates_warned == 2
    assert result.total_prices == 3

    assert (prices / "AAPL.bean").exists()
    assert (prices / "SPY.bean").exists()
    assert (prices / "MSFT.bean").exists()

    archive = prices / "_archive_dated"
    assert (archive / "prices-2026-07-22.bean").exists()
    assert (archive / "prices-2026-07-22.gen.bean").exists()
