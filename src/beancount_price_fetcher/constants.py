"""Module-level defaults; overridable via CLI flags / constructor args.

Per the plan: no config file, but no hidden magic numbers scattered through
the codebase either. Defaults live here as named constants.
"""

from .models import Frequency

DEFAULT_THREAD_COUNT: int = 4
DEFAULT_RETRY_COUNT: int = 3
DEFAULT_FREQUENCY: Frequency = Frequency.DAILY

# Metadata lookup: a fund is classified as Equity/Bond/Cash only when its
# dominant position is at least this fraction of the fund's total assets.
# Mixed funds are left for a human to classify.
FUND_ASSET_CLASS_THRESHOLD: float = 0.80

# Order in which metadata keys are added to a commodity directive.
# Keys are namespaced `yf_` to make it explicit they came from yfinance and to
# avoid colliding with hand-written metadata.
METADATA_KEYS: tuple[str, ...] = (
    "yf_name",
    "yf_quote_type",
    "yf_isin",
    "yf_exchange",
    "yf_currency",
    "yf_asset_class",
    "yf_sector",
    "yf_industry",
    "yf_category",
    "yf_fund_family",
    "yf_expense_ratio",
    "yf_morningstar_rating",
    "yf_market_cap_category",
)
