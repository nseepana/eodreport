"""NSE/BSE trading-day calendar (weekends + trading_holidays.txt)."""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime

from eod_report.session import IST

log = logging.getLogger(__name__)

HOLIDAY_FILE = os.path.join(os.path.dirname(__file__), "..", "trading_holidays.txt")

_holiday_cache: tuple[float, dict[str, frozenset[str] | None]] | None = None


def _load_holidays() -> dict[str, frozenset[str] | None]:
    global _holiday_cache
    try:
        mtime = os.path.getmtime(HOLIDAY_FILE)
    except OSError:
        return {}
    if _holiday_cache is not None and _holiday_cache[0] == mtime:
        return _holiday_cache[1]
    holidays: dict[str, frozenset[str] | None] = {}
    try:
        with open(HOLIDAY_FILE, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", parts[0]):
                    log.warning("trading_calendar.holiday_line_ignored line=%r", raw.strip())
                    continue
                exchanges = (
                    frozenset(e.strip().upper() for e in parts[1].split(",") if e.strip())
                    if len(parts) > 1 else None
                )
                holidays[parts[0]] = exchanges or None
    except OSError as exc:
        log.warning("trading_calendar.holiday_file_unreadable error=%s", exc)
        return {}
    _holiday_cache = (mtime, holidays)
    return holidays


def is_trading_day(exchange: str, ref: datetime | date) -> bool:
    """False on weekends and on listed exchange holidays."""
    if isinstance(ref, datetime):
        d = ref.astimezone(IST).date()
    else:
        d = ref
    if d.weekday() >= 5:
        return False
    closed_for = _load_holidays().get(d.isoformat(), "absent")
    if closed_for == "absent":
        return True
    return not (closed_for is None or exchange.upper() in closed_for)
