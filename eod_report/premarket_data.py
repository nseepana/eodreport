"""Pre-market snapshot builder (mirrors kiteob app/api/market/premarket/route.ts)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

from eod_report.indicators import round2
from eod_report.market_data import _resolve_nifty_session_change
from eod_report.nse_client import (
    get_fii_dii_flows,
    get_gift_nifty,
    get_indices_snapshot,
    get_pre_open_snapshot,
    invalidate_cookie,
)
from eod_report.session import ist_hhmm, ist_now

log = logging.getLogger(__name__)

PREMARKET_GLOBAL_SYMBOLS: list[tuple[str, str, str, str]] = [
    ("^DJI", "dow", "Dow Jones", "US"),
    ("^GSPC", "sp500", "S&P 500", "US"),
    ("^IXIC", "nasdaq", "Nasdaq", "US"),
    ("^VIX", "usvix", "US VIX", "US"),
    ("^FTSE", "ftse", "FTSE 100", "Europe"),
    ("^GDAXI", "dax", "DAX", "Europe"),
    ("^N225", "nikkei", "Nikkei 225", "Asia"),
    ("^HSI", "hangseng", "Hang Seng", "Asia"),
    ("^KS11", "kospi", "Kospi", "Asia"),
    ("INR=X", "usdinr", "USD/INR", "FX"),
    ("DX-Y.NYB", "dxy", "DXY", "FX"),
    ("EURINR=X", "eurinr", "EUR/INR", "FX"),
    ("GC=F", "gold", "Gold", "Commodities"),
    ("SI=F", "silver", "Silver", "Commodities"),
    ("BZ=F", "brent", "Brent crude", "Commodities"),
    ("CL=F", "wti", "WTI crude", "Commodities"),
    ("NG=F", "natgas", "Nat Gas", "Commodities"),
]


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)) and v == v:
        return float(v)
    return None


def compute_index_change_pct(level: dict[str, Any] | None) -> float | None:
    if not level:
        return None
    last = _num(level.get("last"))
    prev = _num(level.get("prev_close"))
    if last is not None and prev:
        return round2((last - prev) / prev * 100)
    cp = _num(level.get("change_pct"))
    return round2(cp) if cp is not None else None


def _median_ieq(stocks: list[dict[str, Any]]) -> float | None:
    vals = sorted(v for s in stocks if (v := _num(s.get("ieq"))) is not None and v > 0)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def build_movers(stocks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    med = _median_ieq(stocks)
    floor = med * 0.2 if med is not None else None

    def flag(s: dict[str, Any]) -> dict[str, Any]:
        ieq = _num(s.get("ieq"))
        low = ieq is None or ieq == 0 or (floor is not None and ieq < floor)
        return {**s, "low_liquidity": low}

    flagged = [flag(s) for s in stocks]
    sorted_rows = sorted(flagged, key=lambda x: x.get("change_pct") or 0, reverse=True)
    gap_ups = [s for s in sorted_rows if (s.get("change_pct") or 0) > 0][:10]
    gap_downs = [s for s in sorted_rows if (s.get("change_pct") or 0) < 0][-10:][::-1]
    return {"gap_ups": gap_ups, "gap_downs": gap_downs}


def derive_bias(change_pct: float | None) -> str:
    if change_pct is None:
        return "Flat"
    if change_pct >= 0.3:
        return "Gap-up"
    if change_pct <= -0.3:
        return "Gap-down"
    return "Flat"


def derive_conviction(
    change_pct: float | None,
    advances: float | None,
    declines: float | None,
) -> str:
    mag = abs(change_pct or 0)
    breadth_agrees = True
    if advances is not None and declines is not None:
        total = advances + declines
        if total > 0:
            ratio = advances / total
            if (change_pct or 0) >= 0:
                breadth_agrees = ratio >= 0.6
            else:
                breadth_agrees = ratio <= 0.4
    if mag >= 0.6 and breadth_agrees:
        return "High"
    if mag >= 0.25:
        return "Medium"
    return "Low"


def _minutes_since_ist(stamp: str) -> float | None:
    m = re.match(r"^(\d{2})-([A-Za-z]{3})-(\d{4})\s+(\d{2}):(\d{2})$", stamp.strip())
    if not m:
        return None
    months = {
        "Jan": 0, "Feb": 1, "Mar": 2, "Apr": 3, "May": 4, "Jun": 5,
        "Jul": 6, "Aug": 7, "Sep": 8, "Oct": 9, "Nov": 10, "Dec": 11,
    }
    mon = months.get(m.group(2))
    if mon is None:
        return None
    # Interpret wall-clock IST as UTC then subtract offset (matches kiteob).
    utc_ms = (
        datetime(int(m.group(3)), mon + 1, int(m.group(1)), int(m.group(4)), int(m.group(5)), tzinfo=timezone.utc).timestamp()
        - 5.5 * 3600
    )
    return (datetime.now(timezone.utc).timestamp() - utc_ms) / 60


def fetch_global_cues() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for symbol, key, label, group in PREMARKET_GLOBAL_SYMBOLS:
        row: dict[str, Any] = {"key": key, "label": label, "group": group}
        try:
            info = yf.Ticker(symbol).info
            row["price"] = _num(info.get("regularMarketPrice"))
            row["change_pct"] = _num(info.get("regularMarketChangePercent"))
        except Exception as exc:  # noqa: BLE001
            log.debug("yahoo %s failed: %s", symbol, exc)
        out.append(row)
    return out


def _ist_clock(now: datetime | None = None) -> dict[str, Any]:
    now = now or ist_now()
    label = now.strftime("%d %b %Y, %H:%M IST")
    return {
        "iso": now.astimezone(timezone.utc).isoformat(),
        "label": label,
        "hhmm": ist_hhmm(now),
    }


def _preopen_phase(hhmm: int, in_window: bool) -> str:
    if not in_window:
        return "closed"
    if hhmm < 908:
        return "order-entry"
    if hhmm < 912:
        return "order-matching"
    return "buffer"


def fetch_premarket_report(now: datetime | None = None) -> dict[str, Any]:
    """Build pre-market JSON matching kiteob /api/market/premarket."""
    now = now or ist_now()
    time = _ist_clock(now)
    hhmm = time["hhmm"]
    in_preopen = 900 <= hhmm <= 915
    preliminary = hhmm < 908
    notes: list[str] = []

    try:
        pre_open = get_pre_open_snapshot("NIFTY")
        indices = get_indices_snapshot()
        flows = get_fii_dii_flows()
        gift = get_gift_nifty()
        if not pre_open.get("stocks") and not (indices.get("nifty50") or {}).get("last"):
            invalidate_cookie()
            pre_open = get_pre_open_snapshot("NIFTY")
            indices = get_indices_snapshot()
            flows = get_fii_dii_flows()
            gift = get_gift_nifty()
    except Exception as exc:  # noqa: BLE001
        log.warning("premarket fetch failed: %s", exc)
        pre_open = {"key": "NIFTY", "stocks": []}
        indices = {}
        flows = []
        gift = None
        notes.append(f"NSE fetch error: {exc}")

    global_cues = fetch_global_cues()
    if not any(c.get("price") is not None for c in global_cues):
        notes.append("Yahoo Finance global cues unavailable.")

    gift_stale_min = _minutes_since_ist(gift["timestamp"]) if gift and gift.get("timestamp") else None
    gift_live = gift_stale_min is not None and gift_stale_min <= 20
    gift_nifty = None
    if gift:
        gift_nifty = {
            **gift,
            "live": gift_live,
            "source": "NSE — GIFT Nifty near-month future",
        }
    else:
        notes.append("NSE GIFT Nifty (marketStatus) unavailable.")
    if gift_nifty and not gift_live:
        notes.append(
            f"GIFT Nifty quote is from {gift.get('timestamp')} IST (last available) — "
            "GIFT pauses 15:40–16:35 IST and on weekends.",
        )

    if not in_preopen:
        notes.append(
            "Outside the 09:00–09:15 IST pre-open window — pre-open figures reflect the last session's data.",
        )
    elif preliminary:
        notes.append("Before 09:08 IST: order entry phase, IEP/IEQ are preliminary and noisy.")

    nifty_change = compute_index_change_pct(indices.get("nifty50"))
    if nifty_change is None:
        vals = [s["change_pct"] for s in pre_open.get("stocks") or [] if s.get("change_pct") is not None]
        nifty_change = round2(sum(vals) / len(vals)) if vals else None

    session_change = _resolve_nifty_session_change(indices, pre_open, gift)
    direction_pct = session_change.get("change_pct") if session_change.get("change_pct") is not None else nifty_change
    bias = derive_bias(direction_pct)
    conviction = derive_conviction(
        direction_pct,
        _num(pre_open.get("advances")),
        _num(pre_open.get("declines")),
    )
    movers = build_movers(pre_open.get("stocks") or [])

    session_date = now.strftime("%Y-%m-%d")
    return {
        "report_date": session_date,
        "as_of": time["iso"],
        "as_of_label": time["label"],
        "window": {
            "in_pre_open_window": in_preopen,
            "preliminary": preliminary,
            "phase": _preopen_phase(hhmm, in_preopen),
        },
        "market_direction": {
            "nifty50": indices.get("nifty50"),
            "bank_nifty": indices.get("bank_nifty"),
            "nifty_change_pct": nifty_change,
            "session_change_pct": direction_pct,
            "session_change_source": session_change.get("source"),
            "bias": bias,
            "conviction": conviction,
            "breadth": {
                "advances": pre_open.get("advances"),
                "declines": pre_open.get("declines"),
                "unchanged": pre_open.get("unchanged"),
            },
            "gift_nifty": gift_nifty,
        },
        "india_vix": indices.get("india_vix"),
        "global_cues": global_cues,
        "flows": flows,
        "gap_ups": movers["gap_ups"],
        "gap_downs": movers["gap_downs"],
        "stock_count": len(pre_open.get("stocks") or []),
        "notes": notes,
    }
