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
    DEFAULT_FILE_EXTENSION,
    PriceWriter,
    append_prices,
    is_dated_filename,
    parse_price_file,
    render_price_line,
)


def test_default_file_extension_is_bean() -> None:
    """The default extension for per-symbol output is .bean."""
    assert DEFAULT_FILE_EXTENSION == ".bean"


def test_render_price_line_basic() -> None:
    line = render_price_line(date(2024, 3, 15), "SPY", Decimal("512.34"), "USD")
    assert line == "2024-03-15 price SPY 512.34 USD"


def test_render_price_line_decimal_quantization() -> None:
    line = render_price_line(date(2024, 3, 15), "SPY", Decimal("512.345678"), "USD")
    assert line == "2024-03-15 price SPY 512.345678 USD"


def test_render_price_line_currency_in_name() -> None:
    line = render_price_line(date(2024, 3, 15), "EUR", Decimal("1.195"), "USD")
    assert line == "2024-03-15 price EUR 1.195 USD"


def test_render_price_line_no_precision_map_leaves_amount_alone() -> None:
    """Without precision_map, the amount is rendered as-is."""
    line = render_price_line(date(2024, 3, 15), "SPY", Decimal("512.345678"), "USD")
    assert line == "2024-03-15 price SPY 512.345678 USD"


def test_render_price_line_with_usd_precision() -> None:
    """USD precision=0.01 quantizes to 2 decimal places."""
    precision = {"USD": Decimal("0.01")}
    line = render_price_line(date(2024, 3, 15), "SPY", Decimal("512.345678"), "USD", precision)
    assert line == "2024-03-15 price SPY 512.35 USD"


def test_render_price_line_with_usd_precision_no_rounding_needed() -> None:
    """When the amount already has 2 decimals, it's left alone."""
    precision = {"USD": Decimal("0.01")}
    line = render_price_line(date(2024, 3, 15), "SPY", Decimal("512.34"), "USD", precision)
    assert line == "2024-03-15 price SPY 512.34 USD"


def test_render_price_line_with_eur_precision() -> None:
    """EUR precision=0.0001 quantizes to 4 decimal places."""
    precision = {"EUR": Decimal("0.0001")}
    line = render_price_line(date(2024, 3, 15), "EUR", Decimal("1.1956789"), "EUR", precision)
    assert line == "2024-03-15 price EUR 1.1957 EUR"


def test_render_price_line_with_jpy_precision() -> None:
    """JPY precision=1 (integer) means no decimals."""
    precision = {"JPY": Decimal("1")}
    line = render_price_line(date(2024, 3, 15), "USDJPY", Decimal("150.42"), "JPY", precision)
    assert line == "2024-03-15 price USDJPY 150 JPY"


def test_render_price_line_precision_applied_per_quote_currency() -> None:
    """Precision is keyed by quote currency; commodity code is irrelevant."""
    precision = {"USD": Decimal("0.01"), "EUR": Decimal("0.0001")}
    usd_line = render_price_line(date(2024, 3, 15), "SPY", Decimal("300.456"), "USD", precision)
    eur_line = render_price_line(date(2024, 3, 15), "SPY", Decimal("300.4567"), "EUR", precision)
    assert usd_line == "2024-03-15 price SPY 300.46 USD"
    assert eur_line == "2024-03-15 price SPY 300.4567 EUR"


def test_render_price_line_precision_uses_bankers_rounding() -> None:
    """ROUND_HALF_EVEN: 0.005 -> 0.00 (not 0.01)."""
    precision = {"USD": Decimal("0.01")}
    line = render_price_line(date(2024, 3, 15), "SPY", Decimal("100.005"), "USD", precision)
    assert line == "2024-03-15 price SPY 100.00 USD"


def test_quantize_for_currency_unknown_currency_passthrough() -> None:
    """Unknown currencies (not in precision_map) keep their full precision."""
    from beancount_price_fetcher.writer import quantize_for_currency

    assert quantize_for_currency(Decimal("512.345678"), "XYZ", {}) == Decimal("512.345678")
    assert quantize_for_currency(Decimal("512.345678"), "XYZ", {"USD": Decimal("0.01")}) == Decimal(
        "512.345678"
    )


def test_quantize_for_currency_known_currency_quantizes() -> None:
    from beancount_price_fetcher.writer import quantize_for_currency

    assert quantize_for_currency(Decimal("512.345678"), "USD", {"USD": Decimal("0.01")}) == Decimal(
        "512.35"
    )


def test_parse_price_file_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "SPY.bean"
    src.write_text(";; header\n2024-01-02 price SPY 300.00 USD\n2024-01-03 price SPY 301.00 USD\n")
    prices = parse_price_file(src)
    assert prices == [
        FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("300.00")),
        FetchedPrice("SPY", "USD", date(2024, 1, 3), Decimal("301.00")),
    ]


def test_parse_price_file_extension_agnostic(tmp_path: Path) -> None:
    """parse_price_file works regardless of extension (.beancount, .bean, ...)."""
    for ext in (".beancount", ".bean", ".gen.bean"):
        src = tmp_path / f"SPY{ext}"
        src.write_text("2024-01-02 price SPY 300.00 USD\n")
        prices = parse_price_file(src)
        assert prices == [FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("300.00"))]


def test_parse_price_file_empty(tmp_path: Path) -> None:
    src = tmp_path / "EMPTY.bean"
    src.write_text(";; just a header, no prices\n")
    assert parse_price_file(src) == []


def test_parse_price_file_ignores_non_price_lines(tmp_path: Path) -> None:
    src = tmp_path / "MIXED.bean"
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


# ---- append_prices (default = preserve order) ----


def test_append_prices_creates_file_with_header(tmp_path: Path) -> None:
    """First write creates the file with a header comment."""
    target = tmp_path / "prices" / "SPY.bean"
    new = [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))]
    result = append_prices(target, new, existing=None)
    assert result is True
    content = target.read_text()
    assert content.startswith(";;")
    assert "Auto-generated by beancount-price-fetcher" in content
    assert "2024-01-05 price SPY 305.00 USD" in content


def test_append_prices_preserves_existing_order(tmp_path: Path) -> None:
    """Default mode: existing lines are NOT re-sorted; new lines appended."""
    target = tmp_path / "SPY.bean"
    target.write_text(
        ";; header\n2024-01-10 price SPY 310.00 USD\n2024-01-02 price SPY 300.00 USD\n"
    )
    new = [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))]
    result = append_prices(target, new, existing=None)
    assert result is True
    content = target.read_text()
    # Existing lines preserved verbatim, in original order (NOT sorted):
    existing_lines = [
        "2024-01-10 price SPY 310.00 USD",
        "2024-01-02 price SPY 300.00 USD",
    ]
    for line in existing_lines:
        assert line in content
    # Verify original order: the "2024-01-10" line appears BEFORE "2024-01-02" in the file
    assert content.index("2024-01-10") < content.index("2024-01-02")
    # New line appended at the end
    assert content.rstrip().endswith("2024-01-05 price SPY 305.00 USD")


def test_append_prises_dedup_within_new(tmp_path: Path) -> None:
    """Duplicate (date, commodity) within the new set is excluded."""
    target = tmp_path / "SPY.bean"
    target.write_text(";; header\n")
    new = [
        FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("300.00")),
        FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("301.00")),
    ]
    result = append_prices(target, new, existing=None)
    assert result is True
    content = target.read_text()
    assert "2024-01-02 price SPY 300.00 USD" in content
    assert "301.00" not in content


def test_append_prices_dedup_against_existing_file(tmp_path: Path) -> None:
    """A date already in the file is deduped out."""
    target = tmp_path / "SPY.bean"
    target.write_text(";; header\n2024-01-02 price SPY 300.00 USD\n")
    new = [FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("301.00"))]
    result = append_prices(target, new, existing=None)
    assert result is False
    content = target.read_text()
    assert "2024-01-02 price SPY 300.00 USD" in content
    assert "301.00" not in content


def test_append_prices_dedup_against_in_memory_existing(tmp_path: Path) -> None:
    target = tmp_path / "SPY.bean"
    target.write_text(";; header\n")
    new = [FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("300.00"))]
    existing = {("SPY", date(2024, 1, 2))}
    result = append_prices(target, new, existing=existing)
    assert result is False
    content = target.read_text()
    assert "2024-01-02" not in content


def test_append_prices_no_new_returns_false(tmp_path: Path) -> None:
    target = tmp_path / "SPY.bean"
    result = append_prices(target, [], existing=None)
    assert result is False
    assert not target.exists()


# ---- append_prices(sort=True) — used by migrate ----


def test_append_prices_sort_true_sorts_by_date(tmp_path: Path) -> None:
    """sort=True re-renders and sorts the entire file."""
    target = tmp_path / "SPY.bean"
    target.write_text(
        ";; header\n2024-01-10 price SPY 310.00 USD\n2024-01-02 price SPY 300.00 USD\n"
    )
    new = [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))]
    result = append_prices(target, new, existing=None, sort=True)
    assert result is True
    content = target.read_text()
    # Lines are now in date order
    price_lines = [ln for ln in content.splitlines() if ln.startswith("2024-")]
    assert price_lines == [
        "2024-01-02 price SPY 300.00 USD",
        "2024-01-05 price SPY 305.00 USD",
        "2024-01-10 price SPY 310.00 USD",
    ]


def test_append_prices_sort_true_dedup(tmp_path: Path) -> None:
    target = tmp_path / "SPY.bean"
    target.write_text(";; header\n2024-01-02 price SPY 300.00 USD\n")
    new = [FetchedPrice("SPY", "USD", date(2024, 1, 2), Decimal("301.00"))]
    result = append_prices(target, new, existing=None, sort=True)
    assert result is False
    assert "301.00" not in target.read_text()


# ---- PriceWriter ----


def test_price_writer_default_extension_is_bean(tmp_path: Path) -> None:
    pw = PriceWriter(prices_dir=tmp_path)
    assert pw.file_extension == ".bean"


def test_price_writer_writes_bean_file(tmp_path: Path) -> None:
    """Default: per-symbol files have .bean extension."""
    pw = PriceWriter(prices_dir=tmp_path)
    prices = [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))]
    written = pw.write_commodity("SPY", prices)
    assert written == 1
    assert (tmp_path / "SPY.bean").exists()
    assert not (tmp_path / "SPY.beancount").exists()


def test_price_writer_custom_extension(tmp_path: Path) -> None:
    """Custom extension produces files like SPY.beancount."""
    pw = PriceWriter(prices_dir=tmp_path, file_extension=".beancount")
    prices = [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))]
    written = pw.write_commodity("SPY", prices)
    assert written == 1
    assert (tmp_path / "SPY.beancount").exists()


def test_price_writer_extension_with_or_without_leading_dot(tmp_path: Path) -> None:
    """Extension accepts with or without a leading dot."""
    pw1 = PriceWriter(prices_dir=tmp_path / "v1", file_extension="bean")
    pw2 = PriceWriter(prices_dir=tmp_path / "v2", file_extension=".bean")
    pw1.write_commodity("X", [FetchedPrice("X", "USD", date(2024, 1, 1), Decimal("1"))])
    pw2.write_commodity("X", [FetchedPrice("X", "USD", date(2024, 1, 1), Decimal("1"))])
    assert (tmp_path / "v1" / "X.bean").exists()
    assert (tmp_path / "v2" / "X.bean").exists()


def test_price_writer_idempotent(tmp_path: Path) -> None:
    """Running twice with the same data doesn't double-write."""
    pw = PriceWriter(prices_dir=tmp_path)
    prices = [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))]
    first = pw.write_commodity("SPY", prices)
    second = pw.write_commodity("SPY", prices)
    assert first == 1
    assert second == 0


def test_price_writer_multiple_commodities(tmp_path: Path) -> None:
    pw = PriceWriter(prices_dir=tmp_path)
    pw.write_commodity("SPY", [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305"))])
    pw.write_commodity("AAPL", [FetchedPrice("AAPL", "USD", date(2024, 1, 5), Decimal("195"))])
    assert (tmp_path / "SPY.bean").exists()
    assert (tmp_path / "AAPL.bean").exists()


def test_price_writer_dedup_across_runs(tmp_path: Path) -> None:
    pw = PriceWriter(prices_dir=tmp_path)
    first = [
        FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305")),
        FetchedPrice("SPY", "USD", date(2024, 1, 8), Decimal("308")),
    ]
    second = [
        FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305")),
        FetchedPrice("SPY", "USD", date(2024, 1, 9), Decimal("309")),
    ]
    pw.write_commodity("SPY", first)
    written = pw.write_commodity("SPY", second)
    assert written == 1
    content = (tmp_path / "SPY.bean").read_text()
    assert "2024-01-05 price SPY 305" in content
    assert "2024-01-08 price SPY 308" in content
    assert "2024-01-09 price SPY 309" in content
    assert content.count("2024-01-05 price SPY") == 1


def test_price_writer_default_preserves_existing_order(tmp_path: Path) -> None:
    """Fetch behavior: append-only, no re-sort of existing content."""
    target = tmp_path / "SPY.bean"
    target.write_text(
        ";; header\n2024-01-10 price SPY 310.00 USD\n2024-01-02 price SPY 300.00 USD\n"
    )
    pw = PriceWriter(prices_dir=tmp_path)
    pw.write_commodity(
        "SPY",
        [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305"))],
    )
    content = target.read_text()
    # Existing 2024-01-10 line preserved BEFORE 2024-01-02 (i.e. not re-sorted)
    assert content.index("2024-01-10") < content.index("2024-01-02")
    assert "2024-01-05" in content


def test_price_writer_sort_output_sorts(tmp_path: Path) -> None:
    """PriceWriter with sort_output=True does re-sort and dedup."""
    target = tmp_path / "SPY.bean"
    target.write_text(
        ";; header\n2024-01-10 price SPY 310.00 USD\n2024-01-02 price SPY 300.00 USD\n"
    )
    pw = PriceWriter(prices_dir=tmp_path, sort_output=True)
    pw.write_commodity(
        "SPY",
        [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305.00"))],
    )
    content = target.read_text()
    price_lines = [ln for ln in content.splitlines() if ln.startswith("2024-")]
    assert price_lines == [
        "2024-01-02 price SPY 300.00 USD",
        "2024-01-05 price SPY 305.00 USD",
        "2024-01-10 price SPY 310.00 USD",
    ]


def test_price_writer_creates_dir(tmp_path: Path) -> None:
    """Writer creates prices/ if it doesn't exist."""
    target = tmp_path / "prices" / "deep"
    pw = PriceWriter(prices_dir=target)
    pw.write_commodity("SPY", [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("305"))])
    assert (target / "SPY.bean").exists()


def test_is_dated_filename_beans() -> None:
    """Both .bean and .gen.bean forms are recognised as bean-price dated files."""
    assert is_dated_filename("prices-2024-01-15.bean")
    assert is_dated_filename("prices-2024-01-15.gen.bean")
    assert not is_dated_filename("SPY.bean")
    assert not is_dated_filename("2024-01-15.bean")


# ---- Display-precision integration ----


def test_price_writer_applies_display_precision(tmp_path: Path) -> None:
    """When display_precision is set on the writer, prices are quantized."""
    pw = PriceWriter(
        prices_dir=tmp_path,
        display_precision={"USD": Decimal("0.01")},
    )
    pw.write_commodity(
        "SPY",
        [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("512.345678"))],
    )
    content = (tmp_path / "SPY.bean").read_text()
    # Quantized to 2 decimal places
    assert "2024-01-05 price SPY 512.35 USD" in content


def test_price_writer_no_precision_keeps_full_value(tmp_path: Path) -> None:
    """Without display_precision, the raw amount is written."""
    pw = PriceWriter(prices_dir=tmp_path)
    pw.write_commodity(
        "SPY",
        [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("512.345678"))],
    )
    content = (tmp_path / "SPY.bean").read_text()
    assert "2024-01-05 price SPY 512.345678 USD" in content


def test_price_writer_precision_applies_only_to_known_currencies(
    tmp_path: Path,
) -> None:
    """Currencies not in display_precision keep full precision."""
    pw = PriceWriter(
        prices_dir=tmp_path,
        display_precision={"USD": Decimal("0.01")},
    )
    pw.write_commodity(
        "SPY",
        [FetchedPrice("SPY", "USD", date(2024, 1, 5), Decimal("195.456"))],
    )
    pw.write_commodity(
        "SPY",
        [FetchedPrice("SPY", "EUR", date(2024, 1, 5), Decimal("195.456789"))],
    )
    # Both calls target SPY.bean. The EUR price has different date+quote;
    # to avoid dedup-on-(commodity, date), use different dates.
    content_usd = (tmp_path / "SPY.bean").read_text()
    assert "2024-01-05 price SPY 195.46 USD" in content_usd

    pw.write_commodity(
        "SPY",
        [FetchedPrice("SPY", "EUR", date(2024, 1, 6), Decimal("195.456789"))],
    )
    content_all = (tmp_path / "SPY.bean").read_text()
    assert "2024-01-05 price SPY 195.46 USD" in content_all
    assert "2024-01-06 price SPY 195.456789 EUR" in content_all  # EUR not in map, full precision
