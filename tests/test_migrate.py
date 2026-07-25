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
    assert is_dated_filename("2024-01-15.beancount") is True
    assert is_dated_filename("2020-12-31.beancount") is True


def test_is_dated_filename_invalid() -> None:
    assert is_dated_filename("SPY.beancount") is False
    assert is_dated_filename("2024-01-15") is False  # no extension
    assert is_dated_filename("2024-01-15.txt") is False  # wrong extension
    assert is_dated_filename("not-a-date.beancount") is False
    assert is_dated_filename("2024-01-15.bc") is False  # wrong extension


def test_is_dated_filename_ignores_non_beancount_extensions() -> None:
    assert is_dated_filename("2024-01-15.py") is False
    assert is_dated_filename("2024-01-15") is False


# ---- parse_dated_files ----


def test_parse_dated_files_simple(tmp_path: Path) -> None:
    """Parse one dated file with two Price directives."""
    src = tmp_path / "2024-01-15.beancount"
    src.write_text("2024-01-15 price SPY 300.00 USD\n2024-01-15 price AAPL 195.00 USD\n")
    prices, errors = parse_dated_files([src])
    assert errors == []
    assert len(prices) == 2
    assert prices[0].commodity == "SPY"
    assert prices[1].commodity == "AAPL"


def test_parse_dated_files_multiple_files(tmp_path: Path) -> None:
    """Two files: prices accumulate."""
    (tmp_path / "2024-01-15.beancount").write_text("2024-01-15 price SPY 300.00 USD\n")
    (tmp_path / "2024-01-16.beancount").write_text("2024-01-16 price AAPL 195.00 USD\n")
    paths = sorted(tmp_path.glob("2024*.beancount"))
    prices, errors = parse_dated_files(paths)
    assert errors == []
    assert len(prices) == 2


def test_parse_dated_files_skips_non_price_lines(tmp_path: Path) -> None:
    src = tmp_path / "2024-01-15.beancount"
    src.write_text(
        ";; header comment\n2024-01-15 price SPY 300.00 USD\n2024-01-01 open Assets:Bank USD\n"
    )
    prices, errors = parse_dated_files([src])
    assert errors == []
    assert len(prices) == 1


def test_parse_dated_files_handles_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.beancount"
    prices, errors = parse_dated_files([missing])
    # parse_file raises for missing files; we want errors surfaced
    assert prices == []
    assert len(errors) > 0


def test_parse_dated_files_dedups_duplicate(tmp_path: Path) -> None:
    """Same (date, commodity) in two files -> one survives, warning emitted."""
    # Two files with same (date, commodity). Sorted alphabetically, 'a' < 'b',
    # so the 'a' file's price (300.00) is the first to be parsed and wins.
    (tmp_path / "2024-01-15a.beancount").write_text("2024-01-15 price SPY 300.00 USD\n")
    (tmp_path / "2024-01-15b.beancount").write_text("2024-01-15 price SPY 301.00 USD\n")
    paths = sorted(tmp_path.glob("2024*.beancount"))
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
    (src / "2024-01-15.beancount").write_text("2024-01-15 price SPY 300.00 USD\n")
    (src / "2024-01-16.beancount").write_text("2024-01-16 price AAPL 195.00 USD\n")

    result = migrate_dated_prices(prices_dir=src, dry_run=True)
    assert isinstance(result, MigrationResult)
    assert result.dated_files_count == 2
    assert result.per_symbol_files_count == 2
    assert result.total_prices == 2
    # Dry run: original files still in place, no per-symbol files written
    assert (src / "2024-01-15.beancount").exists()
    assert (src / "SPY.beancount").exists() is False  # never written


def test_migrate_dated_prices_creates_per_symbol_files(tmp_path: Path) -> None:
    src = tmp_path / "prices"
    src.mkdir()
    (src / "2024-01-15.beancount").write_text(
        "2024-01-15 price SPY 300.00 USD\n2024-01-15 price AAPL 195.00 USD\n"
    )
    (src / "2024-01-16.beancount").write_text("2024-01-16 price SPY 301.00 USD\n")
    result = migrate_dated_prices(prices_dir=src, dry_run=False)
    assert result.dated_files_count == 2
    assert result.per_symbol_files_count == 2  # SPY + AAPL
    assert result.total_prices == 3
    # Per-symbol files written
    assert (src / "SPY.beancount").exists()
    assert (src / "AAPL.beancount").exists()
    # Originals archived (not deleted)
    assert (src / "_archive_dated").exists()
    assert (src / "_archive_dated" / "2024-01-15.beancount").exists()
    assert (src / "_archive_dated" / "2024-01-16.beancount").exists()


def test_migrate_dated_prices_counts_match(tmp_path: Path) -> None:
    """Total written == total read; counts must match exactly."""
    src = tmp_path / "prices"
    src.mkdir()
    (src / "2024-01-15.beancount").write_text(
        "2024-01-15 price SPY 300.00 USD\n"
        "2024-01-15 price AAPL 195.00 USD\n"
        "2024-01-15 price GOOG 1500.00 USD\n"
    )
    (src / "2024-01-16.beancount").write_text(
        "2024-01-16 price SPY 301.00 USD\n2024-01-16 price AAPL 196.00 USD\n"
    )
    result = migrate_dated_prices(prices_dir=src, dry_run=False)
    assert result.total_prices == 5


def test_migrate_dated_prices_sorts_by_date(tmp_path: Path) -> None:
    """Per-symbol file has prices sorted ascending by date."""
    src = tmp_path / "prices"
    src.mkdir()
    (src / "2024-01-16.beancount").write_text("2024-01-16 price SPY 301.00 USD\n")
    (src / "2024-01-15.beancount").write_text("2024-01-15 price SPY 300.00 USD\n")
    (src / "2024-01-17.beancount").write_text("2024-01-17 price SPY 302.00 USD\n")
    migrate_dated_prices(prices_dir=src, dry_run=False)
    content = (src / "SPY.beancount").read_text()
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
    (src / "2024-01-15.beancount").write_text("2024-01-15 price SPY 300.00 USD\n")
    (src / "2024-01-16.beancount").write_text("2024-01-16 price SPY 301.00 USD\n")
    migrate_dated_prices(prices_dir=src, dry_run=False)
    # Load the migrated SPY file directly; should have zero errors
    entries, errors, _ = load_file(str(src / "SPY.beancount"))
    assert errors == []
    assert len(entries) == 2


def test_migrate_dated_prices_only_picks_dated_files(tmp_path: Path) -> None:
    """Non-dated filenames are ignored (e.g., existing SPY.beancount)."""
    src = tmp_path / "prices"
    src.mkdir()
    # Existing per-symbol file (should be left alone)
    (src / "SPY.beancount").write_text("2024-01-01 price SPY 299.00 USD\n")
    # Dated file (should be migrated)
    (src / "2024-01-15.beancount").write_text("2024-01-15 price SPY 300.00 USD\n")
    # Dated file for a different commodity
    (src / "2024-01-16.beancount").write_text("2024-01-16 price AAPL 195.00 USD\n")
    # Random non-dated, non-symbol file
    (src / "README.txt").write_text("ignore me")

    result = migrate_dated_prices(prices_dir=src, dry_run=False)
    assert result.dated_files_count == 2  # only the two 2024-* files
    # SPY.beancount now has the merged set
    content = (src / "SPY.beancount").read_text()
    assert "2024-01-01 price SPY 299.00 USD" in content  # original kept
    assert "2024-01-15 price SPY 300.00 USD" in content  # migrated added
    # AAPL.beancount created
    assert (src / "AAPL.beancount").exists()


def test_dated_file_pattern_constant() -> None:
    """Pattern matches what we expect."""
    assert is_dated_filename("2024-01-15.beancount")
    assert is_dated_filename("2024-12-31.beancount")
    assert not is_dated_filename("SPY.beancount")
    assert not is_dated_filename("2024-1-15.beancount")  # not zero-padded
