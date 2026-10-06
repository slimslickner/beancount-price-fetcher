"""yfinance metadata lookup: quoteType routing, retry, threaded orchestration.

This is the network side of ``fetch-metadata``. It is deliberately separate
from the price fetcher: descriptive metadata is effectively static, so the
command is run ad hoc rather than on a schedule.

Routing is driven by ``info["quoteType"]``:

* ``EQUITY`` -> ``yf_name``, ``yf_asset_class`` = Equity, ``yf_sector``,
  ``yf_industry``, ``yf_market_cap_category``
* ``ETF``/``MUTUALFUND`` -> ``yf_name``, ``yf_category``, ``yf_fund_family``,
  ``yf_expense_ratio``, ``yf_morningstar_rating``, and ``yf_asset_class``
  inferred from the fund breakdown when one position dominates (>= 80%).
  Sector/industry are never set for funds.
* ``CRYPTOCURRENCY`` -> ``yf_name``, ``yf_asset_class`` = Crypto
* anything else -> ``yf_name`` only

``yf_quote_type``/``yf_isin``/``yf_exchange``/``yf_currency`` are set for all
security types.

``funds_data`` and ``Ticker.isin`` are unreliable; any exception while reading
them means "not available", not a lookup failure. A network error on
``Ticker.info`` is a real failure and is retried with exponential backoff.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import yfinance
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .constants import DEFAULT_RETRY_COUNT, DEFAULT_THREAD_COUNT, FUND_ASSET_CLASS_THRESHOLD
from .models import CommodityInfo, MetadataPlan, MetadataStatus, MetadataValue

logger = logging.getLogger(__name__)

_KEY_TO_ATTR: dict[str, str] = {
    "yf_name": "name",
    "yf_quote_type": "quote_type",
    "yf_isin": "isin",
    "yf_exchange": "exchange",
    "yf_currency": "currency",
    "yf_asset_class": "asset_class",
    "yf_sector": "sector",
    "yf_industry": "industry",
    "yf_category": "category",
    "yf_fund_family": "fund_family",
    "yf_expense_ratio": "expense_ratio",
    "yf_morningstar_rating": "morningstar_rating",
    "yf_market_cap_category": "market_cap_category",
}

_FUND_POSITIONS: dict[str, str] = {
    "stockPosition": "Equity",
    "bondPosition": "Bond",
    "cashPosition": "Cash",
}

_MARKET_CAP_BUCKETS: tuple[tuple[float, str], ...] = (
    (200_000_000_000, "Mega Cap"),
    (10_000_000_000, "Large Cap"),
    (2_000_000_000, "Mid Cap"),
    (300_000_000, "Small Cap"),
    (50_000_000, "Micro Cap"),
)


@dataclass(slots=True, frozen=True)
class MetadataLookupRequest:
    """One commodity to look up, with the yfinance symbol to query.

    ``fetch_isin`` gates the extra third-party ISIN request so it is only made
    when ``yf_isin`` is actually among the selected keys.
    """

    commodity: str
    symbol: str
    fetch_isin: bool = True


def get_info_value(info: CommodityInfo, key: str) -> MetadataValue | None:
    """Return the ``CommodityInfo`` field backing a metadata ``key``, or None."""
    attr = _KEY_TO_ATTR.get(key)
    if attr is None:
        return None
    value = getattr(info, attr)
    if isinstance(value, str):
        return value or None
    if isinstance(value, (int, Decimal)):
        return value
    return None


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
        if number is None:
            continue
        if isinstance(raw, str) and raw.strip().endswith("%"):
            number /= 100
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


def _extract_fund_mapping(funds_data: object, attr: str) -> Mapping[str, object] | None:
    """Read a mapping attribute off ``funds_data`` defensively."""
    if funds_data is None:
        return None
    try:
        value: object = getattr(funds_data, attr)
    except Exception as exc:
        logger.debug("funds_data.%s unavailable: %s", attr, exc)
        return None
    if isinstance(value, Mapping):
        return value
    return None


def _fund_family(info: Mapping[str, object], funds_data: object | None) -> str | None:
    """Fund family from ``info``, falling back to ``funds_data.fund_overview``."""
    family = _clean_str(info.get("fundFamily"))
    if family:
        return family
    overview = _extract_fund_mapping(funds_data, "fund_overview")
    if overview is not None:
        return _clean_str(overview.get("family"))
    return None


def _to_int(value: object) -> int | None:
    """Coerce an integral provider value (e.g. a Morningstar rating) to int.

    Rounds half away from zero (``4.5`` -> ``5``) rather than Python's
    banker's rounding, so a provider midpoint does not round down.
    """
    number = _to_float(value)
    if number is None:
        return None
    return int(Decimal(str(number)).to_integral_value(rounding=ROUND_HALF_UP))


def _expense_ratio(info: Mapping[str, object]) -> Decimal | None:
    """Fund expense ratio as a decimal fraction (0.05% -> ``0.0005``), or None.

    yfinance's ``netExpenseRatio`` is already a percentage (SPY 0.0945),
    while ``annualReportExpenseRatio`` is a fraction (VTSAX 0.0004). Both are
    normalised here to a fraction so the stored value is unambiguous.
    """
    net = _to_float(info.get("netExpenseRatio"))
    annual = _to_float(info.get("annualReportExpenseRatio"))
    if net is not None:
        fraction = Decimal(str(net)) / Decimal(100)
    elif annual is not None:
        fraction = Decimal(str(annual))
    else:
        return None
    if fraction < 0:
        return None
    return fraction.normalize()


def _market_cap_category(value: object) -> str | None:
    """Bucket a market capitalisation into a conventional size category."""
    number = _to_float(value)
    if number is None or number <= 0:
        return None
    for threshold, label in _MARKET_CAP_BUCKETS:
        if number >= threshold:
            return label
    return "Nano Cap"


def _clean_isin(value: object) -> str | None:
    """Normalise an ISIN; yfinance uses ``-`` as a "not applicable" sentinel."""
    text = _clean_str(value)
    if text is None or text == "-":
        return None
    return text


def info_to_commodity_info(
    info: object, funds_data: object | None = None, *, isin: object | None = None
) -> CommodityInfo:
    """Route a yfinance ``info`` mapping (and optional fund data) to fields.

    Args:
        info: The ``Ticker.info`` mapping. Non-mappings yield an empty result.
        funds_data: The ``Ticker.funds_data`` object (or a mapping that is
            already the asset-class breakdown) for fund classification.
        isin: The ``Ticker.isin`` value, fetched separately and passed through.

    Returns:
        A ``CommodityInfo`` with only the fields the provider could supply.
    """
    if not isinstance(info, Mapping):
        return CommodityInfo()
    quote_type_raw = _clean_str(info.get("quoteType"))
    quote_type = (quote_type_raw or "").upper()
    name = _clean_str(info.get("longName")) or _clean_str(info.get("shortName"))
    exchange = _clean_str(info.get("fullExchangeName")) or _clean_str(info.get("exchange"))
    currency = _clean_str(info.get("currency"))
    isin_value = _clean_isin(isin)

    if quote_type == "EQUITY":
        return CommodityInfo(
            name=name,
            asset_class="Equity",
            sector=_clean_str(info.get("sector")),
            industry=_clean_str(info.get("industry")),
            market_cap_category=_market_cap_category(info.get("marketCap")),
            quote_type=quote_type_raw,
            exchange=exchange,
            currency=currency,
            isin=isin_value,
        )
    if quote_type in ("ETF", "MUTUALFUND"):
        return CommodityInfo(
            name=name,
            asset_class=_fund_asset_class(_extract_asset_classes(funds_data)),
            category=_clean_str(info.get("category")),
            fund_family=_fund_family(info, funds_data),
            expense_ratio=_expense_ratio(info),
            morningstar_rating=_to_int(info.get("morningStarOverallRating")),
            quote_type=quote_type_raw,
            exchange=exchange,
            currency=currency,
            isin=isin_value,
        )
    if quote_type == "CRYPTOCURRENCY":
        return CommodityInfo(
            name=name,
            asset_class="Crypto",
            quote_type=quote_type_raw,
            exchange=exchange,
            currency=currency,
            isin=isin_value,
        )
    return CommodityInfo(
        name=name,
        quote_type=quote_type_raw,
        exchange=exchange,
        currency=currency,
        isin=isin_value,
    )


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


def lookup_commodity_info(
    symbol: str, retries: int = DEFAULT_RETRY_COUNT, *, fetch_isin: bool = True
) -> CommodityInfo:
    """Look up descriptive metadata for one yfinance ``symbol``.

    Args:
        symbol: The yfinance ticker.
        retries: Per-call retry attempts for the ``info`` request.
        fetch_isin: When True, make the extra (third-party, experimental)
            ``Ticker.isin`` request. Callers should skip it unless
            ``yf_isin`` is actually wanted.

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
    isin: object | None = None
    if fetch_isin:
        try:
            isin = ticker.isin
        except Exception as exc:
            logger.debug("isin unavailable for %s: %s", symbol, exc)
    return info_to_commodity_info(info, funds_data, isin=isin)


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
                pool.submit(
                    lookup_commodity_info, req.symbol, self.retries, fetch_isin=req.fetch_isin
                ): req
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


def _values_equal(provider: MetadataValue, existing: object) -> bool:
    """Type-aware equality between a provider value and a stored metadata value.

    A bare number is never considered equal to a quoted string of the same
    text, so ``--refresh`` rewrites ``"3"`` to ``3`` and ``"0.0004"`` to
    ``0.0004``. Numbers are compared numerically.
    """
    if isinstance(provider, str):
        return isinstance(existing, str) and provider == existing
    if isinstance(existing, (str, bool)) or not isinstance(existing, (int, float, Decimal)):
        return False
    try:
        return Decimal(str(existing)) == Decimal(str(provider))
    except InvalidOperation:
        return False


def plan_metadata_changes(
    commodity: str,
    existing: Mapping[str, object],
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
    adds: list[tuple[str, MetadataValue]] = []
    updates: list[tuple[str, object, MetadataValue]] = []
    unavailable: list[str] = []
    any_value = False

    for key in selected_keys:
        value = get_info_value(info, key)
        if value is not None:
            any_value = True
        if key not in existing:
            if value is not None:
                adds.append((key, value))
            else:
                unavailable.append(key)
        elif refresh and value is not None and not _values_equal(value, existing[key]):
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
