"""Tests for metadata_fetcher: quoteType routing, fund classification, planning.

All network access is mocked by default. The final ``test_live_lookup_smoke``
is opt-in and only runs when ``BEANPRICES_LIVE`` is set.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from beancount_price_fetcher.metadata_fetcher import (
    MetadataFetcher,
    MetadataLookupRequest,
    info_to_commodity_info,
    lookup_commodity_info,
    plan_metadata_changes,
)
from beancount_price_fetcher.models import CommodityInfo, MetadataStatus

ALL_KEYS = ("name", "asset-class", "sector", "industry", "category")


# ---- quoteType routing ----


def test_equity_routes_name_sector_industry() -> None:
    info = {
        "quoteType": "EQUITY",
        "longName": "Apple Inc.",
        "sector": "Technology",
        "industry": "Consumer Electronics",
    }
    result = info_to_commodity_info(info)
    assert result == CommodityInfo(
        name="Apple Inc.",
        asset_class="Equity",
        sector="Technology",
        industry="Consumer Electronics",
    )


def test_equity_falls_back_to_short_name() -> None:
    result = info_to_commodity_info({"quoteType": "EQUITY", "shortName": "Apple"})
    assert result.name == "Apple"


def test_etf_routes_category_and_infers_equity() -> None:
    info = {
        "quoteType": "ETF",
        "longName": "Vanguard Total Stock Market ETF",
        "category": "Large Blend",
    }
    funds = SimpleNamespace(
        asset_classes={"stockPosition": 0.95, "bondPosition": 0.02, "cashPosition": 0.03}
    )
    result = info_to_commodity_info(info, funds)
    assert result == CommodityInfo(
        name="Vanguard Total Stock Market ETF",
        asset_class="Equity",
        category="Large Blend",
    )


def test_mutualfund_routes_category() -> None:
    info = {
        "quoteType": "MUTUALFUND",
        "longName": "Vanguard 500 Index Fund",
        "category": "Large Blend",
    }
    result = info_to_commodity_info(info, None)
    assert result.name == "Vanguard 500 Index Fund"
    assert result.category == "Large Blend"
    assert result.sector is None
    assert result.industry is None


def test_cryptocurrency_routes_name_and_asset_class() -> None:
    result = info_to_commodity_info({"quoteType": "CRYPTOCURRENCY", "longName": "Bitcoin USD"})
    assert result == CommodityInfo(name="Bitcoin USD", asset_class="Crypto")


def test_unknown_quote_type_yields_name_only() -> None:
    result = info_to_commodity_info({"quoteType": "INDEX", "longName": "S&P 500"})
    assert result == CommodityInfo(name="S&P 500")


def test_info_not_a_mapping_yields_empty() -> None:
    assert info_to_commodity_info(None) == CommodityInfo()


# ---- fund asset-class threshold ----


def test_fund_85_percent_stock_maps_to_equity() -> None:
    funds = SimpleNamespace(
        asset_classes={"stockPosition": 0.85, "bondPosition": 0.10, "cashPosition": 0.05}
    )
    result = info_to_commodity_info({"quoteType": "ETF"}, funds)
    assert result.asset_class == "Equity"


def test_fund_60_40_split_leaves_asset_class_unset() -> None:
    funds = SimpleNamespace(asset_classes={"stockPosition": 0.60, "bondPosition": 0.40})
    result = info_to_commodity_info({"quoteType": "ETF"}, funds)
    assert result.asset_class is None


def test_fund_dominant_bond_maps_to_bond() -> None:
    funds = SimpleNamespace(
        asset_classes={"stockPosition": 0.10, "bondPosition": 0.85, "cashPosition": 0.05}
    )
    result = info_to_commodity_info({"quoteType": "MUTUALFUND"}, funds)
    assert result.asset_class == "Bond"


def test_funds_data_raising_leaves_asset_class_unset_without_failing() -> None:
    class ExplodingFunds:
        @property
        def asset_classes(self) -> dict[str, float]:
            raise RuntimeError("yahoo broke")

    result = info_to_commodity_info({"quoteType": "ETF", "longName": "X"}, ExplodingFunds())
    assert result.asset_class is None
    assert result.name == "X"


def test_funds_data_mapping_accepted_directly() -> None:
    result = info_to_commodity_info({"quoteType": "ETF"}, {"stockPosition": 0.9})
    assert result.asset_class == "Equity"


# ---- plan_metadata_changes: add vs update ----


def test_plan_adds_absent_keys() -> None:
    plan = plan_metadata_changes(
        "SPY", {}, CommodityInfo(name="SPY", asset_class="Equity"), ALL_KEYS, refresh=False
    )
    assert plan.status is MetadataStatus.FILLED
    assert plan.adds == (("name", "SPY"), ("asset-class", "Equity"))


def test_plan_leaves_existing_values_untouched_without_refresh() -> None:
    existing = {"name": "Hand written"}
    plan = plan_metadata_changes(
        "SPY",
        existing,
        CommodityInfo(name="Provider name", asset_class="Equity"),
        ALL_KEYS,
        refresh=False,
    )
    assert ("name", "Provider name") not in plan.adds
    assert plan.adds == (("asset-class", "Equity"),)


def test_plan_treats_empty_string_as_present() -> None:
    plan = plan_metadata_changes(
        "AAPL", {"sector": ""}, CommodityInfo(sector="Technology"), ALL_KEYS, refresh=False
    )
    assert ("sector", "Technology") not in plan.adds
    assert plan.updates == ()


def test_plan_refresh_replaces_changed_value() -> None:
    plan = plan_metadata_changes(
        "AAPL", {"sector": "Tech"}, CommodityInfo(sector="Technology"), ALL_KEYS, refresh=True
    )
    assert plan.status is MetadataStatus.UPDATED
    assert plan.updates == (("sector", "Tech", "Technology"),)


def test_plan_refresh_equal_value_is_unchanged() -> None:
    plan = plan_metadata_changes(
        "AAPL",
        {"sector": "Technology"},
        CommodityInfo(sector="Technology"),
        ("sector",),
        refresh=True,
    )
    assert plan.status is MetadataStatus.UNCHANGED
    assert plan.updates == ()


def test_plan_provider_empty_never_blanks_existing() -> None:
    plan = plan_metadata_changes(
        "AAPL", {"sector": "Technology"}, CommodityInfo(), ("sector",), refresh=True
    )
    assert plan.status is MetadataStatus.NOT_FOUND
    assert plan.updates == ()
    assert plan.adds == ()


def test_plan_partial_when_some_selected_keys_unavailable() -> None:
    plan = plan_metadata_changes(
        "SPY", {"name": "SPY"}, CommodityInfo(name="SPY"), ("name", "sector"), refresh=False
    )
    assert plan.status is MetadataStatus.PARTIAL
    assert plan.adds == ()
    assert plan.updates == ()


def test_plan_keys_restrict_adding() -> None:
    plan = plan_metadata_changes(
        "SPY", {}, CommodityInfo(name="SPY", sector="Technology"), ("sector",), refresh=False
    )
    assert plan.adds == (("sector", "Technology"),)


def test_plan_keys_restrict_updating() -> None:
    existing = {"name": "Old", "sector": "Old"}
    plan = plan_metadata_changes(
        "SPY", existing, CommodityInfo(name="New", sector="New"), ("sector",), refresh=True
    )
    assert plan.updates == (("sector", "Old", "New"),)


# ---- lookup + orchestrator (mocked yfinance) ----


class _FakeTicker:
    """Minimal yfinance.Ticker stand-in keyed by symbol."""

    infos: ClassVar[dict[str, Any]] = {}
    funds: ClassVar[dict[str, Any]] = {}
    info_calls: int = 0

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    @property
    def info(self) -> Any:
        type(self).info_calls += 1
        payload = self.infos.get(self.symbol)
        if isinstance(payload, Exception):
            raise payload
        return payload

    @property
    def funds_data(self) -> Any:
        return self.funds.get(self.symbol)


def test_lookup_returns_commodity_info(mocker: Any) -> None:
    _FakeTicker.infos = {"SPY": {"quoteType": "EQUITY", "longName": "SPDR"}}
    _FakeTicker.funds = {}
    mocker.patch("yfinance.Ticker", _FakeTicker)
    result = lookup_commodity_info("SPY", retries=1)
    assert result.name == "SPDR"
    assert result.asset_class == "Equity"


def test_lookup_retries_then_succeeds(mocker: Any) -> None:
    state = {"calls": 0}

    class Flaky:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def info(self) -> Any:
            state["calls"] += 1
            if state["calls"] < 2:
                raise RuntimeError("transient")
            return {"quoteType": "CRYPTOCURRENCY", "longName": "Bitcoin"}

        @property
        def funds_data(self) -> None:
            return None

    mocker.patch("yfinance.Ticker", Flaky)
    result = lookup_commodity_info("BTC-USD", retries=2)
    assert result.asset_class == "Crypto"
    assert state["calls"] == 2


def test_metadata_fetcher_isolates_failures(mocker: Any) -> None:
    _FakeTicker.infos = {
        "GOOD": {"quoteType": "EQUITY", "longName": "Good Inc."},
        "BAD": RuntimeError("nope"),
    }
    _FakeTicker.funds = {}
    mocker.patch("yfinance.Ticker", _FakeTicker)
    fetcher = MetadataFetcher(threads=1, retries=1)
    results = fetcher.fetch_all(
        [MetadataLookupRequest("GOOD", "GOOD"), MetadataLookupRequest("BAD", "BAD")]
    )
    assert isinstance(results["GOOD"], CommodityInfo)
    assert isinstance(results["BAD"], RuntimeError)


def test_metadata_fetcher_threaded(mocker: Any) -> None:
    _FakeTicker.infos = {
        f"T{i}": {"quoteType": "EQUITY", "longName": f"T{i} Inc."} for i in range(6)
    }
    _FakeTicker.funds = {}
    mocker.patch("yfinance.Ticker", _FakeTicker)
    fetcher = MetadataFetcher(threads=4, retries=1)
    results = fetcher.fetch_all([MetadataLookupRequest(f"T{i}", f"T{i}") for i in range(6)])
    assert len(results) == 6
    assert all(isinstance(value, CommodityInfo) for value in results.values())


@pytest.mark.parametrize("quote_type", ["ETF", "MUTUALFUND"])
def test_funds_never_get_sector_or_industry(quote_type: str) -> None:
    info = {
        "quoteType": quote_type,
        "sector": "Technology",
        "industry": "Software",
        "longName": "Fund",
    }
    result = info_to_commodity_info(info, None)
    assert result.sector is None
    assert result.industry is None


# ---- opt-in live smoke test ----
#
# Skipped unless BEANPRICES_LIVE is set, so the default suite stays offline
# and deterministic. Run manually with: BEANPRICES_LIVE=1 uv run pytest ...


@pytest.mark.skipif(
    not os.environ.get("BEANPRICES_LIVE"),
    reason="live network test; set BEANPRICES_LIVE=1 to run",
)
def test_live_lookup_smoke() -> None:
    """Hit Yahoo once for a stable ETF and assert we get usable metadata."""
    info = lookup_commodity_info("SPY", retries=3)
    assert info.name
    assert info.asset_class is not None or info.category is not None
