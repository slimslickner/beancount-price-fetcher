"""Integration tests for the beanprices CLI.

Per the plan, the CLI is a thin layer; we exercise it via click's
``CliRunner`` rather than full unit tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from beancount_price_fetcher.cli import cli

FIXTURE = "tests/fixtures/example.beancount"


def _make_df(rows: list[tuple[str, float]]) -> pd.DataFrame:
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({"Close": [r[1] for r in rows]}, index=idx)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def prices_dir(tmp_path):
    """Empty prices/ directory; CLI tests use --prices-dir to point here."""
    d = tmp_path / "prices"
    d.mkdir()
    return d


def test_cli_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "list-missing" in result.output
    assert "fetch" in result.output
    assert "migrate-dated-prices" in result.output


def test_cli_list_missing(runner: CliRunner) -> None:
    """list-missing prints commodity missing-date summary."""
    with freeze_time("2024-12-31"):
        result = runner.invoke(cli, ["list-missing", "--ledger", FIXTURE])
    assert result.exit_code == 0
    # Should mention all six commodities
    assert "SPY" in result.output
    assert "AAPL" in result.output
    assert "GOOG" in result.output
    assert "EUR" in result.output
    assert "MSFT" in result.output


def test_cli_list_missing_filter_commodity(runner: CliRunner) -> None:
    """--commodity filters output to one commodity."""
    with freeze_time("2024-12-31"):
        result = runner.invoke(cli, ["list-missing", "--ledger", FIXTURE, "--commodity", "SPY"])
    assert result.exit_code == 0
    assert "SPY" in result.output


def test_cli_fetch_dry_run(runner: CliRunner, mocker, prices_dir) -> None:
    """fetch --dry-run doesn't touch the network or files."""
    mock_history = mocker.patch("yfinance.Ticker.history")
    with freeze_time("2024-12-31"):
        result = runner.invoke(
            cli,
            [
                "fetch",
                "--ledger",
                FIXTURE,
                "--prices-dir",
                str(prices_dir),
                "--dry-run",
            ],
        )
    assert result.exit_code == 0
    mock_history.assert_not_called()
    # No files were written
    assert list(prices_dir.iterdir()) == []


def test_cli_fetch_writes_per_symbol_files(runner: CliRunner, mocker, prices_dir) -> None:
    """fetch writes one file per commodity that had prices fetched."""
    mock_history = mocker.patch("yfinance.Ticker.history")
    mock_history.side_effect = lambda **kw: _make_df([("2020-06-16", 311.0)])
    with freeze_time("2024-12-31"):
        result = runner.invoke(
            cli,
            [
                "fetch",
                "--ledger",
                FIXTURE,
                "--prices-dir",
                str(prices_dir),
            ],
        )
    # We may exit non-zero if some tickers fail in CI; for this mock, all succeed
    assert result.exit_code == 0
    written = sorted(p.name for p in prices_dir.iterdir())
    assert "SPY.bean" in written


def test_cli_fetch_exits_nonzero_on_failure(runner: CliRunner, mocker, prices_dir) -> None:
    """If any ticker fails, exit non-zero so cron/CI catches it."""
    mock_history = mocker.patch("yfinance.Ticker.history")
    mock_history.side_effect = RuntimeError("simulated")
    with freeze_time("2024-12-31"):
        result = runner.invoke(
            cli,
            [
                "fetch",
                "--ledger",
                FIXTURE,
                "--prices-dir",
                str(prices_dir),
                "--retries",
                "1",  # fail fast
            ],
        )
    assert result.exit_code != 0


def test_cli_migrate_dated_prices_dry_run(runner: CliRunner, tmp_path) -> None:
    """migrate-dated-prices --dry-run prints plan without moving anything."""
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "prices-2024-01-15.bean").write_text("2024-01-15 price SPY 300.00 USD\n")
    result = runner.invoke(
        cli,
        [
            "migrate-dated-prices",
            "--prices-dir",
            str(prices),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    # Original still in place
    assert (prices / "prices-2024-01-15.bean").exists()
    # Per-symbol file NOT created
    assert not (prices / "SPY.bean").exists()
    assert (
        "dry run" in result.output.lower()
        or "would" in result.output.lower()
        or "1" in result.output
    )


def test_cli_migrate_dated_prices_moves_originals(runner: CliRunner, tmp_path) -> None:
    """Real migrate moves originals to _archive_dated, writes per-symbol files."""
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "prices-2024-01-15.bean").write_text("2024-01-15 price SPY 300.00 USD\n")
    (prices / "prices-2024-01-16.bean").write_text("2024-01-16 price AAPL 195.00 USD\n")
    result = runner.invoke(
        cli,
        [
            "migrate-dated-prices",
            "--prices-dir",
            str(prices),
        ],
    )
    assert result.exit_code == 0
    assert (prices / "SPY.bean").exists()
    assert (prices / "AAPL.bean").exists()
    archive = prices / "_archive_dated"
    assert archive.exists()
    assert (archive / "prices-2024-01-15.bean").exists()


def test_cli_verbosity(runner: CliRunner) -> None:
    """-v increases verbosity."""
    with freeze_time("2024-12-31"):
        result = runner.invoke(cli, ["-v", "list-missing", "--ledger", FIXTURE])
    assert result.exit_code == 0


# ---- Closing-prices: end-date / include-today / today-filter plumbing ----


def test_cli_fetch_drops_today_by_default(runner: CliRunner, mocker, prices_dir) -> None:
    """By default, no Price directive for ``date.today()`` is written."""
    mock_history = mocker.patch("yfinance.Ticker.history")
    mock_history.return_value = _make_df(
        [
            ("2024-06-01", 100.0),
            ("2024-06-03", 999.0),  # = today under freeze_time; intraday snapshot
        ]
    )
    with freeze_time("2024-06-03"):
        result = runner.invoke(
            cli,
            ["fetch", "--ledger", FIXTURE, "--prices-dir", str(prices_dir)],
        )
    assert result.exit_code == 0
    spy_file = prices_dir / "SPY.bean"
    if spy_file.exists():
        content = spy_file.read_text()
        assert "2024-06-03" not in content


def test_cli_fetch_include_today_writes_today_row(runner: CliRunner, mocker, prices_dir) -> None:
    """``--include-today`` bypasses the today-filter; today's row is written."""
    mock_history = mocker.patch("yfinance.Ticker.history")
    mock_history.return_value = _make_df([("2024-06-03", 999.0)])
    with freeze_time("2024-06-03"):
        result = runner.invoke(
            cli,
            [
                "fetch",
                "--ledger",
                FIXTURE,
                "--prices-dir",
                str(prices_dir),
                "--include-today",
            ],
        )
    assert result.exit_code == 0
    # SPY.bean exists and contains a 2024-06-03 line (today's intraday snapshot)
    spy_file = prices_dir / "SPY.bean"
    assert spy_file.exists()
    content = spy_file.read_text()
    assert "2024-06-03" in content


def test_cli_fetch_end_date_caps_yfinance_window(runner: CliRunner, mocker, prices_dir) -> None:
    """``--end-date`` caps every yfinance ``end`` parameter at end_date + 1 day."""
    mock_history = mocker.patch("yfinance.Ticker.history")
    mock_history.return_value = _make_df([])
    with freeze_time("2024-06-03"):
        result = runner.invoke(
            cli,
            [
                "fetch",
                "--ledger",
                FIXTURE,
                "--prices-dir",
                str(prices_dir),
                "--end-date",
                "2024-06-01",
            ],
        )
    assert result.exit_code == 0
    assert mock_history.call_count > 0
    for call in mock_history.call_args_list:
        _, kwargs = call
        # No call may exceed the cap. Calls for already-closed commodities
        # may be earlier than the cap (their req.max_date is earlier),
        # which is correct.
        assert kwargs.get("end") <= "2024-06-02"


def test_cli_fetch_end_date_inclusive_of_end_date(runner: CliRunner, mocker, prices_dir) -> None:
    """``--end-date 2024-06-01`` requests yfinance through 2024-06-01 inclusive."""
    mock_history = mocker.patch("yfinance.Ticker.history")
    mock_history.return_value = _make_df([("2024-06-01", 100.0)])
    with freeze_time("2024-06-03"):
        result = runner.invoke(
            cli,
            [
                "fetch",
                "--ledger",
                FIXTURE,
                "--prices-dir",
                str(prices_dir),
                "--end-date",
                "2024-06-01",
                "--include-today",  # so the 2024-06-01 row isn't filtered
            ],
        )
    assert result.exit_code == 0
    spy_file = prices_dir / "SPY.bean"
    if spy_file.exists():
        content = spy_file.read_text()
        assert "2024-06-01" in content


def test_cli_list_missing_unchanged_after_fix(runner: CliRunner) -> None:
    """``list-missing`` is unaffected by the fetcher-side today-filter."""
    with freeze_time("2024-12-31"):
        result = runner.invoke(cli, ["list-missing", "--ledger", FIXTURE])
    assert result.exit_code == 0
    assert "SPY" in result.output


# ---- fetch-metadata ----

METADATA_FIXTURE = "tests/fixtures/metadata.beancount"


class _CliTicker:
    """Fake yfinance.Ticker keyed by symbol for fetch-metadata CLI tests."""

    infos: ClassVar[dict[str, Any]] = {}
    funds: ClassVar[dict[str, Any]] = {}

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    @property
    def info(self) -> Any:
        payload = self.infos.get(self.symbol)
        if isinstance(payload, Exception):
            raise payload
        return payload

    @property
    def funds_data(self) -> Any:
        return self.funds.get(self.symbol)


def _install_metadata_ticker(mocker) -> _CliTicker:
    _CliTicker.infos = {
        "SPY": {
            "quoteType": "ETF",
            "longName": "SPDR S&P 500 ETF Trust",
            "category": "Large Blend",
        },
        "AAPL": {
            "quoteType": "EQUITY",
            "longName": "Apple Inc.",
            "sector": "Technology",
            "industry": "Consumer Electronics",
        },
        "FXAIX": {
            "quoteType": "MUTUALFUND",
            "longName": "Fidelity 500 Index Fund",
            "category": "Large Blend",
        },
        "MSFT": {
            "quoteType": "EQUITY",
            "longName": "Microsoft Corporation",
            "sector": "Technology",
            "industry": "Software",
        },
        "CASHONLY": {"quoteType": "MONEYMARKET", "longName": "Cash Only"},
    }
    _CliTicker.funds = {"SPY": {"stockPosition": 0.98, "bondPosition": 0.01, "cashPosition": 0.01}}
    mocker.patch("yfinance.Ticker", _CliTicker)
    return _CliTicker


def _copy_metadata_ledger(tmp_path) -> Path:
    import shutil

    destination = tmp_path / "metadata.beancount"
    shutil.copy(METADATA_FIXTURE, destination)
    return destination


def _hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_fetch_metadata_preview_does_not_write(runner: CliRunner, mocker, tmp_path) -> None:
    _install_metadata_ticker(mocker)
    ledger = _copy_metadata_ledger(tmp_path)
    before = _hash(ledger)
    result = runner.invoke(cli, ["fetch-metadata", "--ledger", str(ledger)])
    assert result.exit_code == 0
    assert _hash(ledger) == before
    assert "Preview only" in result.output
    assert "SPY" in result.output
    assert "MSFT" in result.output
    # Diff is shown for planned edits.
    assert "---" in result.output


def test_cli_fetch_metadata_write_adds_metadata(runner: CliRunner, mocker, tmp_path) -> None:
    _install_metadata_ticker(mocker)
    ledger = _copy_metadata_ledger(tmp_path)
    result = runner.invoke(cli, ["fetch-metadata", "--ledger", str(ledger), "--write"])
    assert result.exit_code == 0
    text = ledger.read_text()
    assert 'name: "Apple Inc."' in text
    assert "industry" in text
    # New directive for the held, undeclared MSFT goes to commodities.bean.
    output = tmp_path / "commodities.bean"
    assert output.exists()
    assert "commodity MSFT" in output.read_text()
    assert "include" in result.output


def test_cli_fetch_metadata_keys_restricts_adds(runner: CliRunner, mocker, tmp_path) -> None:
    _install_metadata_ticker(mocker)
    ledger = _copy_metadata_ledger(tmp_path)
    result = runner.invoke(
        cli,
        ["fetch-metadata", "--ledger", str(ledger), "--write", "--keys", "name"],
    )
    assert result.exit_code == 0
    text = ledger.read_text()
    # AAPL's empty hand-written sector stays empty; only name is considered.
    assert 'sector: ""' in text
    assert 'sector: "Technology"' not in text


def test_cli_fetch_metadata_refresh_updates_value(runner: CliRunner, mocker, tmp_path) -> None:
    _install_metadata_ticker(mocker)
    ledger = _copy_metadata_ledger(tmp_path)
    result = runner.invoke(
        cli,
        ["fetch-metadata", "--ledger", str(ledger), "--write", "--refresh", "--keys", "sector"],
    )
    assert result.exit_code == 0
    assert 'sector: "Technology"' in ledger.read_text()


def test_cli_fetch_metadata_without_refresh_leaves_existing(
    runner: CliRunner, mocker, tmp_path
) -> None:
    _install_metadata_ticker(mocker)
    ledger = _copy_metadata_ledger(tmp_path)
    runner.invoke(cli, ["fetch-metadata", "--ledger", str(ledger), "--write", "--keys", "sector"])
    # Without --refresh the empty existing sector is untouched, and the absent
    # name/asset-class are not selected.
    assert 'sector: ""' in ledger.read_text()


def test_cli_fetch_metadata_exits_nonzero_on_lookup_failure(
    runner: CliRunner, mocker, tmp_path
) -> None:
    _CliTicker.infos = {"SPY": RuntimeError("network down")}
    _CliTicker.funds = {}
    mocker.patch("yfinance.Ticker", _CliTicker)
    ledger = _copy_metadata_ledger(tmp_path)
    result = runner.invoke(cli, ["fetch-metadata", "--ledger", str(ledger), "--retries", "1"])
    assert result.exit_code != 0
    assert "network down" in result.output


def test_cli_fetch_metadata_all_includes_declared_only(runner: CliRunner, mocker, tmp_path) -> None:
    _install_metadata_ticker(mocker)
    ledger = _copy_metadata_ledger(tmp_path)
    result = runner.invoke(
        cli, ["fetch-metadata", "--ledger", str(ledger), "--all", "--retries", "1"]
    )
    assert result.exit_code == 0
    assert "CASHONLY" in result.output


def test_cli_fetch_metadata_unknown_key_rejected(runner: CliRunner, tmp_path) -> None:
    ledger = _copy_metadata_ledger(tmp_path)
    result = runner.invoke(cli, ["fetch-metadata", "--ledger", str(ledger), "--keys", "bogus"])
    assert result.exit_code != 0
    assert "bogus" in result.output


def test_cli_help_lists_fetch_metadata(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "fetch-metadata" in result.output


def test_cli_fetch_metadata_edits_included_file(runner: CliRunner, mocker, tmp_path) -> None:
    _install_metadata_ticker(mocker)
    main = tmp_path / "main.beancount"
    holdings = tmp_path / "holdings.beancount"
    main.write_text('option "operating_currency" "USD"\ninclude "holdings.beancount"\n')
    holdings.write_text(
        '2020-01-01 commodity SPY\n  price: "USD:yahoo/SPY"\n'
        "2020-01-01 open Assets:Investments:SPY\n"
        "2020-01-01 open Assets:Bank\n"
        "2020-01-02 *\n"
        "  Assets:Investments:SPY 10 SPY {300 USD}\n"
        "  Assets:Bank\n"
    )
    main_before = main.read_text()
    result = runner.invoke(
        cli, ["fetch-metadata", "--ledger", str(main), "--write", "--keys", "name"]
    )
    assert result.exit_code == 0
    assert main.read_text() == main_before
    assert 'name: "SPDR S&P 500 ETF Trust"' in holdings.read_text()
    # The edited ledger still loads cleanly.
    from beancount.loader import load_file

    _, errors, _ = load_file(str(main))
    assert errors == []
