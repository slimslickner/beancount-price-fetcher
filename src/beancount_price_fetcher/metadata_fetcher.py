"""yfinance metadata lookup: quoteType routing, retry, threaded orchestration.

This is the network side of ``fetch-metadata``. It is deliberately separate
from the price fetcher: descriptive metadata is effectively static, so the
command is run ad hoc rather than on a schedule.

Routing is driven by ``info["quoteType"]``:

* ``EQUITY`` -> name, asset-class ``Equity``, sector, industry
* ``ETF``/``MUTUALFUND`` -> name, category, and an asset-class inferred from
  the fund breakdown when one position dominates (>= 80%). Sector/industry
  are never set for funds.
* ``CRYPTOCURRENCY`` -> name, asset-class ``Crypto``
* anything else -> name only

``funds_data`` is unreliable; any exception while reading it means "no fund
breakdown", not a lookup failure. A network error on ``Ticker.info`` is a
real failure and is retried with exponential backoff.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import yfinance
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .constants import DEFAULT_RETRY_COUNT, DEFAULT_THREAD_COUNT, FUND_ASSET_CLASS_THRESHOLD
from .models import CommodityInfo, MetadataPlan, MetadataStatus

logger = logging.getLogger(__name__)

_KEY_TO_ATTR: dict[str, str] = {
    "name": "name",
    "asset-class": "asset_class",
    "sector": "sector",
    "industry": "industry",
    "category": "category",
}

_FUND_POSITIONS: dict[str, str] = {
    "stockPosition": "Equity",
    "bondPosition": "Bond",
    "cashPosition": "Cash",
}


@dataclass(slots=True, frozen=True)
class MetadataLookupRequest:
    """One commodity to look up, with the yfinance symbol to query."""

    commodity: str
    symbol: str


def get_info_value(info: CommodityInfo, key: str) -> str | None:
    """Return the ``CommodityInfo`` field backing a metadata ``key``, or None."""
    attr = _KEY_TO_ATTR.get(key)
    if attr is None:
        return None
    value = getattr(info, attr)
    return value if isinstance(value, str) and value else None


def _clean_str(value: object) -> str | None:
    """Coerce a provider value to a non-empty stripped string, or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_float(value: object) -> float | None:
    """Coerce a fund-position value (float, int, numeric str, ``"40%"``) to float."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().rstrip("%")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _fund_asset_class(asset_classes: object) -> str | None:
    """Infer Equity/Bond/Cash from a fund breakdown, or None if mixed/unknown.

    Takes the largest of the stock/bond/cash positions and maps it only when
    it is at least ``FUND_ASSET_CLASS_THRESHOLD`` of the total of all
    reported positions. Anything else stays unset so a human can decide.
    """
    if not isinstance(asset_classes, Mapping):
        return None
    values: dict[str, float] = {}
    for key, raw in asset_classes.items():
        number = _to_float(raw)
        if number is not None:
            values[str(key)] = number
    if not values:
        return None
    total = sum(values.values())
    if total <= 0:
        return None
    positions = {key: values.get(key, 0.0) for key in _FUND_POSITIONS}
    largest = max(positions, key=lambda key: positions[key])
    if positions[largest] <= 0:
        return None
    if positions[largest] / total >= FUND_ASSET_CLASS_THRESHOLD:
        return _FUND_POSITIONS[largest]
    return None


def _extract_asset_classes(funds_data: object) -> object | None:
    """Read ``funds_data.asset_classes`` defensively.

    Accepts either a mapping that already is the breakdown, or an object
    exposing an ``asset_classes`` attribute. Any exception means "no fund
    breakdown".
    """
    if funds_data is None:
        return None
    if isinstance(funds_data, Mapping):
        return funds_data
    try:
        value: object = funds_data.asset_classes  # type: ignore[attr-defined]
        return value
    except Exception as exc:
        logger.debug("funds_data unavailable: %s", exc)
        return None


def info_to_commodity_info(info: object, funds_data: object | None = None) -> CommodityInfo:
    """Route a yfinance ``info`` mapping (and optional fund data) to fields.

    Args:
        info: The ``Ticker.info`` mapping. Non-mappings yield an empty result.
        funds_data: The ``Ticker.funds_data`` object (or a mapping that is
            already the asset-class breakdown) for fund classification.

    Returns:
        A ``CommodityInfo`` with only the fields the provider could supply.
    """
    if not isinstance(info, Mapping):
        return CommodityInfo()
    quote_type = (_clean_str(info.get("quoteType")) or "").upper()
    name = _clean_str(info.get("longName")) or _clean_str(info.get("shortName"))

    if quote_type == "EQUITY":
        return CommodityInfo(
            name=name,
            asset_class="Equity",
            sector=_clean_str(info.get("sector")),
            industry=_clean_str(info.get("industry")),
        )
    if quote_type in ("ETF", "MUTUALFUND"):
        return CommodityInfo(
            name=name,
            asset_class=_fund_asset_class(_extract_asset_classes(funds_data)),
            category=_clean_str(info.get("category")),
        )
    if quote_type == "CRYPTOCURRENCY":
        return CommodityInfo(name=name, asset_class="Crypto")
    return CommodityInfo(name=name)


def _fetch_info_with_retry(ticker: Any, retries: int) -> object:
    """Call ``ticker.info`` with tenacity retry/backoff."""

    @retry(
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def _do() -> object:
        return ticker.info

    return _do()


def lookup_commodity_info(symbol: str, retries: int = DEFAULT_RETRY_COUNT) -> CommodityInfo:
    """Look up descriptive metadata for one yfinance ``symbol``.

    Args:
        symbol: The yfinance ticker.
        retries: Per-call retry attempts for the ``info`` request.

    Returns:
        The mapped ``CommodityInfo``. Missing fields are left as None.

    Raises:
        Exception: Re-raised from yfinance after all retries are exhausted.
    """
    ticker = yfinance.Ticker(symbol)
    info = _fetch_info_with_retry(ticker, retries)
    funds_data: object | None
    try:
        funds_data = ticker.funds_data
    except Exception as exc:
        logger.debug("funds_data unavailable for %s: %s", symbol, exc)
        funds_data = None
    return info_to_commodity_info(info, funds_data)


@dataclass(slots=True)
class MetadataFetcher:
    """Batch metadata fetcher running all lookups in a thread pool.

    Per-ticker failures are isolated: one failing symbol never aborts the
    batch. Results map commodity code to either a ``CommodityInfo`` or the
    exception raised after retries were exhausted.
    """

    threads: int = DEFAULT_THREAD_COUNT
    retries: int = DEFAULT_RETRY_COUNT

    def fetch_all(
        self,
        requests: Sequence[MetadataLookupRequest],
        *,
        on_result: Callable[[str], None] | None = None,
    ) -> dict[str, CommodityInfo | Exception]:
        """Look up every request in parallel; return per-commodity outcomes.

        Args:
            requests: Commodities/symbols to look up.
            on_result: Optional callback invoked, in the caller's thread, with
                each commodity code as its lookup finishes. Used for progress
                reporting; failures are reported too.
        """
        results: dict[str, CommodityInfo | Exception] = {}
        if not requests:
            return results
        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            future_to_req = {
                pool.submit(lookup_commodity_info, req.symbol, self.retries): req
                for req in requests
            }
            for future in as_completed(future_to_req):
                req = future_to_req[future]
                try:
                    results[req.commodity] = future.result()
                except Exception as exc:
                    logger.warning("metadata lookup failed for %s: %s", req.symbol, exc)
                    results[req.commodity] = exc
                if on_result is not None:
                    on_result(req.commodity)
        return results


def plan_metadata_changes(
    commodity: str,
    existing: Mapping[str, str],
    info: CommodityInfo,
    selected_keys: Sequence[str],
    *,
    refresh: bool,
) -> MetadataPlan:
    """Decide which metadata keys to add or update for one commodity.

    Add semantics (no ``--refresh``): a key is added only when absent;
    present values (including empty strings) are left alone. With
    ``--refresh``, a present key is replaced only when the provider supplies
    a different non-empty value. Nothing is ever blanked out.

    Status priority: NOT_FOUND (provider gave nothing usable at all),
    FILLED, UPDATED, PARTIAL (some needed key unavailable and nothing was
    applied), UNCHANGED.
    """
    adds: list[tuple[str, str]] = []
    updates: list[tuple[str, str, str]] = []
    unavailable: list[str] = []
    any_value = False

    for key in selected_keys:
        value = get_info_value(info, key)
        if value:
            any_value = True
        if key not in existing:
            if value:
                adds.append((key, value))
            else:
                unavailable.append(key)
        elif refresh and value and value != existing[key]:
            updates.append((key, existing[key], value))

    if not any_value:
        status = MetadataStatus.NOT_FOUND
    elif adds:
        status = MetadataStatus.FILLED
    elif updates:
        status = MetadataStatus.UPDATED
    elif unavailable:
        status = MetadataStatus.PARTIAL
    else:
        status = MetadataStatus.UNCHANGED

    return MetadataPlan(
        commodity=commodity,
        status=status,
        adds=tuple(adds),
        updates=tuple(updates),
    )
