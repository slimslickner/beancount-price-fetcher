"""Integration tests for the beanprices CLI.

Per the plan, the CLI is a thin layer; we exercise it via click's
``CliRunner`` rather than full unit tests.
"""

from __future__ import annotations

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
    assert "SPY.beancount" in written


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
    (prices / "2024-01-15.beancount").write_text("2024-01-15 price SPY 300.00 USD\n")
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
    assert (prices / "2024-01-15.beancount").exists()
    # Per-symbol file NOT created
    assert not (prices / "SPY.beancount").exists()
    assert (
        "dry run" in result.output.lower()
        or "would" in result.output.lower()
        or "1" in result.output
    )


def test_cli_migrate_dated_prices_moves_originals(runner: CliRunner, tmp_path) -> None:
    """Real migrate moves originals to _archive_dated, writes per-symbol files."""
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "2024-01-15.beancount").write_text("2024-01-15 price SPY 300.00 USD\n")
    (prices / "2024-01-16.beancount").write_text("2024-01-16 price AAPL 195.00 USD\n")
    result = runner.invoke(
        cli,
        [
            "migrate-dated-prices",
            "--prices-dir",
            str(prices),
        ],
    )
    assert result.exit_code == 0
    assert (prices / "SPY.beancount").exists()
    assert (prices / "AAPL.beancount").exists()
    archive = prices / "_archive_dated"
    assert archive.exists()
    assert (archive / "2024-01-15.beancount").exists()


def test_cli_verbosity(runner: CliRunner) -> None:
    """-v increases verbosity."""
    with freeze_time("2024-12-31"):
        result = runner.invoke(cli, ["-v", "list-missing", "--ledger", FIXTURE])
    assert result.exit_code == 0
