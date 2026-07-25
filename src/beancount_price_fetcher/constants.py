"""Module-level defaults; overridable via CLI flags / constructor args.

Per the plan: no config file, but no hidden magic numbers scattered through
the codebase either. Defaults live here as named constants.
"""

from .models import Frequency

DEFAULT_THREAD_COUNT: int = 4
DEFAULT_RETRY_COUNT: int = 3
DEFAULT_FREQUENCY: Frequency = Frequency.DAILY
