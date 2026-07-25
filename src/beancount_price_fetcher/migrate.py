"""One-time migration: dated ``prices/YYYY-MM-DD.beancount`` -> per-symbol files.

Reads every dated price file under ``prices/``, groups all Price directives
by commodity, writes per-symbol files via ``writer.py``, and archives the
originals to ``prices/_archive_dated/`` (NOT deleted; rollback for free).

The plan also asks for a verification step (reload new files, confirm
zero errors). That's built into ``migrate_dated_prices``.
"""

from __future__ import annotations

import logging
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from beancount.core import data
from beancount.loader import load_file
from beancount.parser import parser

from .models import FetchedPrice
from .writer import PriceWriter, is_dated_filename

logger = logging.getLogger(__name__)

ARCHIVE_DIR_NAME = "_archive_dated"


@dataclass(slots=True, frozen=True)
class MigrationResult:
    """Summary of a migrate_dated_prices run."""

    dated_files_count: int
    per_symbol_files_count: int
    total_prices: int
    dry_run: bool
    duplicates_warned: int = 0
    new_files_loaded_errors: int = 0


def parse_dated_files(paths: list[Path]) -> tuple[list[FetchedPrice], list[Any]]:
    """Read every ``Price`` directive from each file; dedup; warn on collisions.

    Returns:
        (prices, errors) -- ``prices`` is the deduped list (first occurrence
        wins); ``errors`` includes parse issues and duplicate warnings.
    """
    prices: list[FetchedPrice] = []
    seen: set[tuple[str, date]] = set()
    errors: list[Any] = []
    for path in paths:
        if not path.exists():
            msg = f"file not found: {path}"
            logger.warning(msg)
            errors.append(msg)
            continue
        entries, parse_errors, _ = parser.parse_file(str(path))
        for err in parse_errors[:5]:
            logger.warning("parse error in %s: %s", path, err.message)
            errors.append(err)
        for entry in entries:
            if isinstance(entry, data.Price):
                key = (entry.currency, entry.date)
                if key in seen:
                    msg = f"duplicate price ({entry.currency}, {entry.date}) in {path}"
                    logger.warning(msg)
                    errors.append(msg)
                    continue
                seen.add(key)
                prices.append(
                    FetchedPrice(
                        commodity=entry.currency,
                        quote_currency=entry.amount.currency,
                        date=entry.date,
                        price=Decimal(str(entry.amount.number)),
                    )
                )
    return prices, errors


def migrate_dated_prices(
    prices_dir: str | Path,
    *,
    dry_run: bool = False,
) -> MigrationResult:
    """Migrate dated price files to per-symbol files.

    Steps:
        1. Find all dated files in ``prices_dir``.
        2. Parse them, dedup, warn on duplicates.
        3. Write per-symbol files via ``PriceWriter``.
        4. Verify the new files load with zero errors.
        5. Move originals to ``prices_dir/_archive_dated/`` (NOT delete).

    Args:
        prices_dir: Path to the prices directory.
        dry_run: If True, report what would happen without writing or moving.

    Returns:
        MigrationResult with counts.
    """
    prices_dir = Path(prices_dir)
    if not prices_dir.exists():
        msg = f"prices dir not found: {prices_dir}"
        raise FileNotFoundError(msg)

    dated_paths = sorted(
        p for p in prices_dir.iterdir() if p.is_file() and is_dated_filename(p.name)
    )

    prices, errors = parse_dated_files(dated_paths)
    duplicates_warned = sum(1 for e in errors if isinstance(e, str) and "duplicate" in e)

    by_commodity: dict[str, list[FetchedPrice]] = defaultdict(list)
    for fp in prices:
        by_commodity[fp.commodity].append(fp)

    per_symbol_count = len(by_commodity)
    total_prices = len(prices)

    if dry_run:
        return MigrationResult(
            dated_files_count=len(dated_paths),
            per_symbol_files_count=per_symbol_count,
            total_prices=total_prices,
            dry_run=True,
            duplicates_warned=duplicates_warned,
        )

    writer = PriceWriter(prices_dir=prices_dir)
    for commodity, fp_list in by_commodity.items():
        writer.write_commodity(commodity, fp_list)

    new_load_errors = 0
    for commodity in by_commodity:
        target = prices_dir / f"{commodity}.beancount"
        if target.exists():
            _entries, errs, _ = load_file(str(target))
            if errs:
                new_load_errors += len(errs)
                logger.warning("parse errors in migrated %s: %d", target, len(errs))

    archive_dir = prices_dir / ARCHIVE_DIR_NAME
    archive_dir.mkdir(exist_ok=True)
    for p in dated_paths:
        target = archive_dir / p.name
        if target.exists():
            i = 1
            while target.exists():
                target = archive_dir / f"{p.stem}_{i}{p.suffix}"
                i += 1
        shutil.move(str(p), str(target))

    return MigrationResult(
        dated_files_count=len(dated_paths),
        per_symbol_files_count=per_symbol_count,
        total_prices=total_prices,
        dry_run=False,
        duplicates_warned=duplicates_warned,
        new_files_loaded_errors=new_load_errors,
    )


__all__ = [
    "ARCHIVE_DIR_NAME",
    "MigrationResult",
    "is_dated_filename",
    "migrate_dated_prices",
    "parse_dated_files",
]
