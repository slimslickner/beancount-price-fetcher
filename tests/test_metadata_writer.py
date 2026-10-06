"""Tests for metadata_writer: text edits to commodity directives."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from beancount.loader import load_file

from beancount_price_fetcher.ledger import extract_commodity_directives
from beancount_price_fetcher.metadata_writer import (
    DirectiveEdit,
    NewMetadataDirective,
    append_new_directives,
    apply_directive_edits,
)


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "ledger.beancount"
    path.write_text(content)
    return path


# ---- insertion ----


def test_insert_when_directive_has_no_metadata(tmp_path: Path) -> None:
    path = _write(tmp_path, "2020-01-01 commodity SPY\n")
    result = apply_directive_edits(
        path, [DirectiveEdit("SPY", 1, adds=(("name", "SPY"),))], write=True
    )
    assert result.changed is True
    assert path.read_text() == '2020-01-01 commodity SPY\n  name: "SPY"\n'
    assert "-" in result.diff and "name" in result.diff


def test_insert_after_existing_metadata(tmp_path: Path) -> None:
    path = _write(tmp_path, '2020-01-01 commodity SPY\n  price: "USD:yahoo/SPY"\n')
    apply_directive_edits(path, [DirectiveEdit("SPY", 1, adds=(("name", "SPY"),))], write=True)
    assert path.read_text() == (
        '2020-01-01 commodity SPY\n  price: "USD:yahoo/SPY"\n  name: "SPY"\n'
    )


def test_insert_after_indented_comment(tmp_path: Path) -> None:
    path = _write(tmp_path, '2020-01-01 commodity SPY\n  price: "USD:yahoo/SPY"\n  ; note\n')
    apply_directive_edits(path, [DirectiveEdit("SPY", 1, adds=(("name", "SPY"),))], write=True)
    assert path.read_text() == (
        '2020-01-01 commodity SPY\n  price: "USD:yahoo/SPY"\n  ; note\n  name: "SPY"\n'
    )


def test_insert_uses_tab_indentation(tmp_path: Path) -> None:
    path = _write(tmp_path, '2020-01-01 commodity SPY\n\tprice: "USD:yahoo/SPY"\n')
    apply_directive_edits(path, [DirectiveEdit("SPY", 1, adds=(("name", "SPY"),))], write=True)
    assert path.read_text() == (
        '2020-01-01 commodity SPY\n\tprice: "USD:yahoo/SPY"\n\tname: "SPY"\n'
    )


def test_insert_when_directive_is_last_line_without_newline(tmp_path: Path) -> None:
    path = _write(tmp_path, "2020-01-01 commodity SPY")
    apply_directive_edits(path, [DirectiveEdit("SPY", 1, adds=(("name", "SPY"),))], write=True)
    assert path.read_text() == '2020-01-01 commodity SPY\n  name: "SPY"'


# ---- in-place replace ----


def test_replace_preserves_indent_and_trailing_comment(tmp_path: Path) -> None:
    path = _write(tmp_path, '2020-01-01 commodity AAPL\n  sector: "Tech" ; hand note\n')
    apply_directive_edits(
        path, [DirectiveEdit("AAPL", 1, updates=(("sector", "Technology"),))], write=True
    )
    assert path.read_text() == '2020-01-01 commodity AAPL\n  sector: "Technology" ; hand note\n'


def test_replace_is_quote_aware_about_semicolons(tmp_path: Path) -> None:
    path = _write(tmp_path, '2020-01-01 commodity AAPL\n  name: "Apple ; Inc." ; note\n')
    apply_directive_edits(
        path, [DirectiveEdit("AAPL", 1, updates=(("name", "New ; Name"),))], write=True
    )
    assert path.read_text() == '2020-01-01 commodity AAPL\n  name: "New ; Name" ; note\n'


def test_replace_unquoted_value_with_quoted(tmp_path: Path) -> None:
    path = _write(tmp_path, "2020-01-01 commodity AAPL\n  sector: Technology\n")
    apply_directive_edits(
        path, [DirectiveEdit("AAPL", 1, updates=(("sector", "Technology"),))], write=True
    )
    assert path.read_text() == '2020-01-01 commodity AAPL\n  sector: "Technology"\n'


def test_duplicate_key_warns_and_edits_first(tmp_path: Path, caplog) -> None:
    path = _write(tmp_path, '2020-01-01 commodity AAPL\n  sector: "A"\n  sector: "B"\n')
    with caplog.at_level(logging.WARNING):
        apply_directive_edits(
            path, [DirectiveEdit("AAPL", 1, updates=(("sector", "C"),))], write=True
        )
    assert path.read_text() == '2020-01-01 commodity AAPL\n  sector: "C"\n  sector: "B"\n'
    assert any("duplicate" in record.message for record in caplog.records)


# ---- multiple directives / mixed edits ----


def test_mixed_inserts_and_replaces_across_directives(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '2020-01-01 commodity SPY\n  name: "Old"\n'
        '2021-01-01 commodity AAPL\n  price: "USD:yahoo/AAPL"\n',
    )
    apply_directive_edits(
        path,
        [
            DirectiveEdit("SPY", 1, updates=(("name", "New"),)),
            DirectiveEdit("AAPL", 3, adds=(("sector", "Technology"),)),
        ],
        write=True,
    )
    # Bottom-to-top application must keep the earlier directive's line valid.
    assert path.read_text() == (
        '2020-01-01 commodity SPY\n  name: "New"\n'
        '2021-01-01 commodity AAPL\n  price: "USD:yahoo/AAPL"\n  sector: "Technology"\n'
    )


def test_insert_and_update_on_same_directive(tmp_path: Path) -> None:
    path = _write(tmp_path, '2020-01-01 commodity SPY\n  name: "Old"\n')
    apply_directive_edits(
        path,
        [DirectiveEdit("SPY", 1, adds=(("sector", "Technology"),), updates=(("name", "New"),))],
        write=True,
    )
    assert path.read_text() == ('2020-01-01 commodity SPY\n  name: "New"\n  sector: "Technology"\n')


# ---- safety ----


def test_stale_lineno_aborts_file_without_writing(tmp_path: Path) -> None:
    path = _write(tmp_path, "2020-01-01 commodity SPY\n")
    result = apply_directive_edits(
        path, [DirectiveEdit("SPY", 5, adds=(("name", "SPY"),))], write=True
    )
    assert result.errors
    assert result.changed is False
    assert path.read_text() == "2020-01-01 commodity SPY\n"


def test_mismatched_commodity_on_lineno_aborts_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "2020-01-01 commodity SPY\n")
    result = apply_directive_edits(
        path, [DirectiveEdit("AAPL", 1, adds=(("name", "AAPL"),))], write=True
    )
    assert result.errors
    assert path.read_text() == "2020-01-01 commodity SPY\n"


def test_preview_does_not_write(tmp_path: Path) -> None:
    path = _write(tmp_path, "2020-01-01 commodity SPY\n")
    before = path.read_text()
    result = apply_directive_edits(
        path, [DirectiveEdit("SPY", 1, adds=(("name", "SPY"),))], write=False
    )
    assert result.changed is True
    assert result.diff != ""
    assert path.read_text() == before


def test_new_directive_escapes_quotes_and_backslashes(tmp_path: Path) -> None:
    path = tmp_path / "commodities.bean"
    value = 'He said "hi" \\ there'
    append_new_directives(
        path,
        [NewMetadataDirective("SPY", date(2020, 1, 1), (("name", value),))],
        write=True,
    )
    assert path.read_text() == ('2020-01-01 commodity SPY\n  name: "He said \\"hi\\" \\\\ there"\n')


# ---- append new directives ----


def test_append_new_directive_creates_file(tmp_path: Path) -> None:
    path = tmp_path / "commodities.bean"
    result = append_new_directives(
        path,
        [
            NewMetadataDirective(
                "AAPL", date(2021, 1, 15), (("name", "Apple Inc."), ("asset-class", "Equity"))
            )
        ],
        write=True,
    )
    assert result.changed is True
    assert path.read_text() == (
        '2021-01-15 commodity AAPL\n  name: "Apple Inc."\n  asset-class: "Equity"\n'
    )


def test_append_skips_existing_directive(tmp_path: Path) -> None:
    path = tmp_path / "commodities.bean"
    path.write_text('2021-01-15 commodity AAPL\n  name: "Apple Inc."\n')
    before = path.read_text()
    result = append_new_directives(
        path,
        [NewMetadataDirective("AAPL", date(2021, 1, 15), (("name", "Apple Inc."),))],
        write=True,
    )
    assert result.changed is False
    assert path.read_text() == before


def test_append_separates_from_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "commodities.bean"
    path.write_text('2021-01-15 commodity AAPL\n  name: "Apple Inc."\n')
    append_new_directives(
        path,
        [NewMetadataDirective("MSFT", date(2021, 6, 1), (("name", "Microsoft"),))],
        write=True,
    )
    assert path.read_text() == (
        '2021-01-15 commodity AAPL\n  name: "Apple Inc."\n'
        '\n2021-06-01 commodity MSFT\n  name: "Microsoft"\n'
    )


def test_append_does_not_write_on_preview(tmp_path: Path) -> None:
    path = tmp_path / "commodities.bean"
    result = append_new_directives(
        path,
        [NewMetadataDirective("AAPL", date(2021, 1, 15), (("name", "Apple Inc."),))],
        write=False,
    )
    assert result.changed is True
    assert not path.exists()


# ---- round trip ----


def test_round_trip_ledger_loads_and_metadata_reads_back(tmp_path: Path) -> None:
    path = _write(tmp_path, '2020-01-01 commodity SPY\n  price: "USD:yahoo/SPY"\n')
    apply_directive_edits(
        path,
        [
            DirectiveEdit(
                "SPY", 1, adds=(("name", "SPDR S&P 500 ETF Trust"), ("asset-class", "Equity"))
            )
        ],
        write=True,
    )
    entries, errors, _ = load_file(str(path))
    assert errors == []
    directives = extract_commodity_directives(entries)
    assert directives["SPY"].metadata["name"] == "SPDR S&P 500 ETF Trust"
    assert directives["SPY"].metadata["asset-class"] == "Equity"
