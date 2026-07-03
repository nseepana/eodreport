"""IST trading-session date helpers (mirrors kiteob/lib/indian-market-session-date + eod-market-data)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
IST_SESSION_OPEN_HHMM = 915
IST_SESSION_CLOSE_HHMM = 1530


def ist_now() -> datetime:
    return datetime.now(tz=IST)


def ist_hhmm(now: datetime | None = None) -> int:
    now = now or ist_now()
    return now.hour * 100 + now.minute


def ist_today_iso(now: datetime | None = None) -> str:
    return (now or ist_now()).strftime("%Y-%m-%d")


def _parse_iso(s: str) -> date:
    y, m, d = (int(x) for x in s.split("-"))
    return date(y, m, d)


def _fmt_iso(d: date) -> str:
    return d.isoformat()


def is_weekend_iso(ymd: str) -> bool:
    return _parse_iso(ymd).weekday() >= 5


def prev_weekday_iso(ymd: str) -> str:
    d = _parse_iso(ymd) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return _fmt_iso(d)


def next_trading_session_iso(from_iso: str) -> str:
    d = _parse_iso(from_iso) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return _fmt_iso(d)


def last_completed_session_iso(now: datetime | None = None) -> str:
    now = now or ist_now()
    today = now.strftime("%Y-%m-%d")
    if now.weekday() < 5 and ist_hhmm(now) >= IST_SESSION_CLOSE_HHMM:
        return today
    return prev_weekday_iso(today)


def resolve_target_session(requested: str, now: datetime | None = None) -> str:
    now = now or ist_now()
    if requested and len(requested) == 10 and requested[4] == "-":
        return next_trading_session_iso(requested) if is_weekend_iso(requested) else requested
    today = ist_today_iso(now)
    if now.weekday() < 5 and ist_hhmm(now) < IST_SESSION_CLOSE_HHMM:
        return today
    return next_trading_session_iso(today)


def data_session_for_target(target_iso: str, now: datetime | None = None) -> str:
    last = last_completed_session_iso(now)
    if target_iso <= last:
        return target_iso
    prev = prev_weekday_iso(target_iso)
    return last if prev > last else prev


def is_live_data_session(data_session: str, now: datetime | None = None) -> bool:
    now = now or ist_now()
    return data_session == last_completed_session_iso(now) and not market_open_now(now)


def market_open_now(now: datetime | None = None) -> bool:
    now = now or ist_now()
    if now.weekday() >= 5:
        return False
    hhmm = ist_hhmm(now)
    return IST_SESSION_OPEN_HHMM <= hhmm < IST_SESSION_CLOSE_HHMM


def is_kite_quote_ohlc_ready(now: datetime | None = None) -> bool:
    now = now or ist_now()
    if now.weekday() >= 5:
        return False
    return ist_hhmm(now) >= IST_SESSION_OPEN_HHMM


def is_overnight_ist(now: datetime | None = None) -> bool:
    hhmm = ist_hhmm(now)
    return hhmm >= IST_SESSION_CLOSE_HHMM or hhmm < IST_SESSION_OPEN_HHMM
