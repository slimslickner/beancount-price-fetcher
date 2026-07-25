"""Tests for beancount_price_fetcher.writer against temp files.

Per plan: tests for file-I/O components use temp files rather than the
shared fixture, so this module builds its own scratch files via tmp_path.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount_price_fetcher.models import FetchedPrice
from beancount_price_fetcher.writer import (
    PriceWriter,
    append_and_sort,
    parse_price_file,
    render_price_line,
)


def test_render_price_line_basic() -> None:
    line = render_price_line(date(2024, 3, 15), "SPY", Decimal("512.34"), "USD")
    assert line == "2024-03-15 price SPY 512.34 USD"


def test_render_price_line_decimal_quantization() -> None:
    """Prices get quantized to 2 decimal places (beancount convention)."""
    line = render_price_line(date(2024, 3, 15), "SPY", Decimal("512.345678"), "USD")
    assert line == "2024-03-15 price SPY 512.345678 USD"
    # Note: we preserve precision here; beancount will accept any Decimal.


def test_render_price_line_currency_in_name() -> None:
    """EUR priced in USD works as expected."""
    line = render_price_line(date(2024, 3, 15), "EUR", Decimal("1.195"), "USD")
    assert line == "2024-03-15 price EUR 1.195 USD"


def test_parse_price_file_roundtrip(tmp_path: Path) -> None:
    """Parse what we just wrote."""
    src = tmp_path / "SPY.beancount"
    src.write_text(";; header\n2024-01-02 price SPY 300.00 USD\n2024-01-03 price SPY 301.00 USD\n")
    prices = parse_price_file(src)
    assert prices == [
        FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("300.00")),
        FetchedPrice("SPY", "USD", date(2024, 1, 3), Decimal("301.00")),
    ]


def test_parse_price_file_empty(tmp_path: Path) -> None:
    src = tmp_path / "EMPTY.beancount"
    src.write_text(";; just a header, no prices\n")
    assert parse_price_file(src) == []


def test_parse_price_file_ignores_non_price_lines(tmp_path: Path) -> None:
    src = tmp_path / "MIXED.beancount"
    src.write_text(
        "2024-01-02 price SPY 300.00 USD\n"
        ";; comment line\n"
        "2024-01-01 open Assets:Bank USD\n"
        "2024-01-03 price SPY 301.00 USD\n"
    )
    prices = parse_price_file(src)
    assert len(prices) == 2
    assert prices[0].date == date(2024, 1, 2)
    assert prices[1].date == date(2024, 1, 3)


def test_append_and_sort_creates_file_with_header(tmp_path: Path) -> None:
    """First write creates the file with a header comment."""
    target = tmp_path / "prices" / "SPY.beancount"
    new = [
        FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00")),
    ]
    result = append_and_sort(target, new, existing=None)
    assert result is True  # something was written
    content = target.read_text()
    assert content.startswith(";;")
    assert "SPY" in content
    assert "2024-01-05 price SPY 305.00 USD" in content


def test_append_and_sort_appends_to_existing(tmp_path: Path) -> None:
    target = tmp_path / "SPY.beancount"
    target.write_text(";; header\n2024-01-02 price SPY 300.00 USD\n")
    new = [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))]
    result = append_and_sort(target, new, existing=None)
    assert result is True
    content = target.read_text()
    # Existing line preserved
    assert "2024-01-02 price SPY 300.00 USD" in content
    # New line added (and since 2024-01-05 > 2024-01-02, it goes after)
    assert "2024-01-05 price SPY 305.00 USD" in content


def test_append_and_sort_resorts_to_date_order(tmp_path: Path) -> None:
    """Even if input is out of order, output is sorted by date ascending."""
    target = tmp_path / "SPY.beancount"
    target.write_text(";; header\n2024-01-10 price SPY 310.00 USD\n")
    new = [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))]
    append_and_sort(target, new, existing=None)
    content = target.read_text()
    # Lines should be in date order
    lines = [line for line in content.splitlines() if line.startswith("2024")]
    assert lines == [
        "2024-01-05 price SPY 305.00 USD",
        "2024-01-10 price SPY 310.00 USD",
    ]


def test_append_and_sort_dedup(tmp_path: Path) -> None:
    """Existing duplicate is excluded; only the new entry survives."""
    target = tmp_path / "SPY.beancount"
    target.write_text(";; header\n2024-01-02 price SPY 300.00 USD\n")
    # Caller asks to add (2024-01-02) again with a different price
    new = [FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("301.00"))]
    result = append_and_sort(target, new, existing=None)
    # Nothing was written (already present)
    assert result is False
    content = target.read_text()
    # Original line preserved
    assert "2024-01-02 price SPY 300.00 USD" in content
    assert "301.00" not in content


def test_append_and_sort_dedup_against_in_memory_existing(tmp_path: Path) -> None:
    """Existing-prices set provided by caller is also dedup'd against."""
    target = tmp_path / "SPY.beancount"
    target.write_text(";; header\n")
    new = [FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("300.00"))]
    existing = {("SPY", date(2024, 1, 2))}  # caller says already in ledger
    result = append_and_sort(target, new, existing=existing)
    assert result is False
    content = target.read_text()
    assert "2024-01-02" not in content


def test_append_and_sort_no_new_returns_false(tmp_path: Path) -> None:
    """Empty new list returns False."""
    target = tmp_path / "SPY.beancount"
    result = append_and_sort(target, [], existing=None)
    assert result is False
    assert not target.exists()


# ---- PriceWriter class tests ----


def test_price_writer_writes_file(tmp_path: Path) -> None:
    """End-to-end: PriceWriter writes a file."""
    pw = PriceWriter(prices_dir=tmp_path)
    prices = [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))]
    written = pw.write_commodity("SPY", prices)
    assert written == 1
    assert (tmp_path / "SPY.beancount").exists()


def test_price_writer_idempotent(tmp_path: Path) -> None:
    """Running twice with the same data doesn't double-write."""
    pw = PriceWriter(prices_dir=tmp_path)
    prices = [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))]
    first = pw.write_commodity("SPY", prices)
    second = pw.write_commodity("SPY", prices)
    assert first == 1
    assert second == 0  # nothing new


def test_price_writer_multiple_commodities(tmp_path: Path) -> None:
    pw = PriceWriter(prices_dir=tmp_path)
    pw.write_commodity("SPY", [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305"))])
    pw.write_commodity("AAPL", [FetchedPrice("AAPL", "USD", date(2024, 1, 5), Decimal("195"))])
    assert (tmp_path / "SPY.beancount").exists()
    assert (tmp_path / "AAPL.beancount").exists()


def test_price_writer_dedup_across_runs(tmp_path: Path) -> None:
    """Second run with overlapping dates only writes the new dates."""
    pw = PriceWriter(prices_dir=tmp_path)
    first = [
        FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305")),
        FetchedPrice("SPY", "USD", date(2024, 1, 8), Decimal("308")),
    ]
    second = [
        FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305")),  # dup
        FetchedPrice("SPY", "USD", date(2024, 1, 9), Decimal("309")),  # new
    ]
    pw.write_commodity("SPY", first)
    written = pw.write_commodity("SPY", second)
    assert written == 1
    content = (tmp_path / "SPY.beancount").read_text()
    assert "2024-01-05 price SPY 305" in content
    assert "2024-01-08 price SPY 308" in content
    assert "2024-01-09 price SPY 309" in content
    # 2024-01-05 appears exactly once
    assert content.count("2024-01-05 price SPY") == 1


def test_price_writer_creates_dir(tmp_path: Path) -> None:
    """Writer creates prices/ if it doesn't exist."""
    target = tmp_path / "prices" / "deep"
    pw = PriceWriter(prices_dir=target)
    pw.write_commodity("SPY", [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305"))])
    assert (target / "SPY.beancount").exists()
