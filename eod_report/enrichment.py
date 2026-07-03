"""Computed movers, breadth, technical levels, and derivatives enrichment."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from eod_report.indicators import (
    classic_pivot,
    ema_series,
    pivot_signal,
    round2,
    rsi_wilder,
    trend_from_emas,
)
from eod_report.kite_throttle import pause_before_kite_historical
from eod_report.market_data import (
    YAHOO_BANK_NIFTY,
    YAHOO_NIFTY_50,
    completed_session_bars,
    fetch_kite_session_ohlc_map,
    _instrument_token,
    _ist_range,
    _kite_client,
    _normalize_key,
)
from eod_report.nse_client import get_nifty50_movers, get_option_chain_metrics, invalidate_cookie
from eod_report.session import is_kite_quote_ohlc_ready

log = logging.getLogger(__name__)

NIFTY_50_SYMBOLS = [
    "COALINDIA", "HDFCBANK", "ICICIBANK", "RELIANCE", "BHARTIARTL", "ADANIENT", "AXISBANK",
    "ETERNAL", "HINDALCO", "TATASTEEL", "ONGC", "WIPRO", "SBIN", "ITC", "INFY", "LT", "TMPV",
    "INDIGO", "TCS", "NTPC", "BAJFINANCE", "BEL", "M&M", "MARUTI", "ADANIPORTS", "KOTAKBANK",
    "SHRIRAMFIN", "BAJAJ-AUTO", "POWERGRID", "ULTRACEMCO", "TECHM", "JSWSTEEL", "MAXHEALTH",
    "SUNPHARMA", "APOLLOHOSP", "ASIANPAINT", "JIOFIN", "EICHERMOT", "TITAN", "TATACONSUM",
    "TRENT", "NESTLEIND", "HINDUNILVR", "CIPLA", "DRREDDY", "HCLTECH", "GRASIM", "HDFCLIFE",
    "SBILIFE", "BAJAJFINSV",
]


def _last_number(series: list[float | None]) -> float | None:
    for v in reversed(series):
        if isinstance(v, (int, float)) and v == v:
            return float(v)
    return None


def _bars_through_session(bars: list[dict[str, Any]], session_ymd: str) -> list[dict[str, Any]]:
    picked = completed_session_bars(bars, session_ymd)
    if not picked:
        return bars
    return bars[: picked["session_index"] + 1]


def _bar_ymd(bar: dict[str, Any]) -> str:
    t = bar.get("date") or bar.get("time", "")
    if hasattr(t, "strftime"):
        return t.strftime("%Y-%m-%d")
    return str(t)[:10]


def technical_levels_from_bars(bars: list[dict[str, Any]], session_ymd: str) -> dict[str, Any] | None:
    bars = _bars_through_session(bars, session_ymd)
    if len(bars) < 30:
        return None
    picked = completed_session_bars(bars, session_ymd)
    if not picked:
        return None
    session = picked["session"]
    prev = picked.get("prev")
    closes = [float(b["close"]) for b in bars]
    last_close = float(session["close"])
    prev_close = float(prev["close"]) if prev else None
    change_pct = round2(((last_close - prev_close) / prev_close) * 100) if prev_close else None
    rsi = rsi_wilder(closes, 14)
    ema20 = _last_number(ema_series(closes, 20))
    ema50 = _last_number(ema_series(closes, 50))
    ema200 = _last_number(ema_series(closes, 200)) if len(closes) >= 200 else None
    pv = classic_pivot(float(session["high"]), float(session["low"]), last_close)
    return {
        "close": round2(last_close),
        "change_pct": change_pct,
        "session_ohlc": {
            "open": round2(float(session["open"])),
            "high": round2(float(session["high"])),
            "low": round2(float(session["low"])),
            "close": round2(last_close),
        },
        "rsi_daily": rsi,
        "supports": [pv.s1, pv.s2],
        "resistances": [pv.r1, pv.r2],
        "pivot": pv.pivot,
        "pivot_signal": pivot_signal(last_close, pv),
        "ema20": round2(ema20) if ema20 is not None else None,
        "ema50": round2(ema50) if ema50 is not None else None,
        "ema200": round2(ema200) if ema200 is not None else None,
        "trend": trend_from_emas(last_close, ema20, ema50),
    }


def fetch_yahoo_daily_bars(symbol: str, session_ymd: str, lookback: int = 400) -> list[dict[str, Any]]:
    import yfinance as yf

    end = datetime.strptime(session_ymd, "%Y-%m-%d") + timedelta(days=1)
    start = end - timedelta(days=lookback)
    hist = yf.Ticker(symbol).history(start=start, end=end, interval="1d")
    bars: list[dict[str, Any]] = []
    if hist.empty:
        return bars
    for idx, row in hist.reset_index().iterrows():
        d = row["Date"]
        ymd = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        if ymd > session_ymd:
            continue
        bars.append({
            "date": ymd,
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        })
    return bars


def _compute_technical_levels_kite(access_token: str, key: str, session_ymd: str) -> dict[str, Any] | None:
    client = _kite_client(access_token)
    if not client:
        return None
    token = _instrument_token(client, key)
    if token is None:
        return None
    from_d, to_d = _ist_range(400)
    try:
        pause_before_kite_historical()
        bars = client.get_historical_data(token, from_d, to_d, "day")
        return technical_levels_from_bars(bars, session_ymd)
    except Exception as exc:  # noqa: BLE001
        log.debug("kite technical levels %s: %s", key, exc)
        return None


def _resolve_technical_levels(
    access_token: str | None,
    kite_key: str,
    yahoo_symbol: str,
    session_ymd: str,
    *,
    prefer_yahoo: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    if access_token and not prefer_yahoo:
        fact = _compute_technical_levels_kite(access_token, kite_key, session_ymd)
        if fact:
            return fact, "Kite historical (RSI, pivots, EMA trend)"
    try:
        bars = fetch_yahoo_daily_bars(yahoo_symbol, session_ymd)
        fact = technical_levels_from_bars(bars, session_ymd)
        if fact:
            return fact, f"Yahoo Finance ({yahoo_symbol} RSI, pivots, EMA trend)"
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _compute_top_movers_historical(access_token: str, session_ymd: str) -> dict[str, Any] | None:
    keys = [f"NSE:{s}" for s in NIFTY_50_SYMBOLS]
    from_d, to_d = _ist_range(14)
    client = _kite_client(access_token)
    if not client:
        return None

    def task(sym: str) -> dict[str, Any] | None:
        key = _normalize_key(f"NSE:{sym}")
        token = _instrument_token(client, key)
        if token is None:
            return None
        try:
            pause_before_kite_historical()
            bars = client.get_historical_data(token, from_d, to_d, "day")
            picked = completed_session_bars(bars, session_ymd)
            if not picked or not picked.get("prev"):
                return None
            prev = float(picked["prev"]["close"])
            last = float(picked["session"]["close"])
            if not prev:
                return None
            return {"stock": sym, "change_pct": round2(((last - prev) / prev) * 100)}
        except Exception:  # noqa: BLE001
            return None

    rows: list[dict[str, Any]] = []
    for sym in NIFTY_50_SYMBOLS:
        r = task(sym)
        if r:
            rows.append(r)
    if not rows:
        return None
    return _movers_from_rows(rows)


def _movers_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    adv = dec = unch = 0
    for r in rows:
        p = r["change_pct"]
        if p > 0.05:
            adv += 1
        elif p < -0.05:
            dec += 1
        else:
            unch += 1
    sorted_rows = sorted(rows, key=lambda x: x["change_pct"], reverse=True)
    ratio = f"{adv / dec:.2f}:1" if dec else (f"{adv}:0" if adv else "0:0")
    return {
        "movers": {"gainers": sorted_rows[:5], "losers": list(reversed(sorted_rows[-5:]))},
        "breadth": {"advances": adv, "declines": dec, "unchanged": unch, "ratio": ratio},
    }


def _compute_top_movers_live(access_token: str, session_ymd: str) -> dict[str, Any] | None:
    keys = [f"NSE:{s}" for s in NIFTY_50_SYMBOLS]
    ohlc_map = fetch_kite_session_ohlc_map(access_token, keys)
    if not ohlc_map:
        return None
    rows: list[dict[str, Any]] = []
    for sym in NIFTY_50_SYMBOLS:
        row = ohlc_map.get(_normalize_key(f"NSE:{sym}"))
        if not row or not row.get("prev_close"):
            continue
        pct = ((row["last_price"] - row["prev_close"]) / row["prev_close"]) * 100
        rows.append({"stock": sym, "change_pct": round2(pct)})
    if not rows:
        return None
    return _movers_from_rows(rows)


def _compute_top_movers(access_token: str, session_ymd: str, use_live_quote: bool) -> dict[str, Any] | None:
    if use_live_quote and is_kite_quote_ohlc_ready():
        live = _compute_top_movers_live(access_token, session_ymd)
        if live:
            return live
    # Prior-session close: NSE index constituents are authoritative — avoid 50× Kite historical.
    if not use_live_quote:
        return None
    return _compute_top_movers_historical(access_token, session_ymd)


def _derivatives_from_chain(metrics: dict[str, Any]) -> dict[str, Any]:
    pcr = metrics.get("pcr")
    oi_trend = None
    if pcr is not None:
        if pcr > 1.2:
            oi_trend = f"Put OI dominant (PCR {pcr}) — support building near put wall {metrics.get('key_put_wall') or '—'}"
        elif pcr < 0.8:
            oi_trend = f"Call OI dominant (PCR {pcr}) — resistance near call wall {metrics.get('key_call_wall') or '—'}"
        else:
            oi_trend = f"Balanced OI (PCR {pcr}) — max pain {metrics.get('max_pain') or '—'}"
    return {
        "pcr": pcr,
        "max_pain": metrics.get("max_pain"),
        "key_call_wall": metrics.get("key_call_wall"),
        "key_put_wall": metrics.get("key_put_wall"),
        "oi_expiry": metrics.get("expiry"),
        "oi_trend": oi_trend,
    }


def _compute_derivatives() -> dict[str, Any] | None:
    for attempt in range(2):
        try:
            metrics = get_option_chain_metrics("NIFTY")
            if metrics:
                return _derivatives_from_chain(metrics)
        except Exception:  # noqa: BLE001
            if attempt == 0:
                invalidate_cookie()
    return None


def _resolve_nearest_nifty_future(client, underlying: str = "NIFTY") -> str | None:
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        candidates: list[tuple[str, str]] = []
        for row in client.instruments("NFO"):
            if row.get("instrument_type") != "FUT":
                continue
            if (row.get("name") or "").upper() != underlying:
                continue
            ts = (row.get("tradingsymbol") or "").upper()
            if not ts.endswith("FUT"):
                continue
            exp = row.get("expiry")
            ymd = exp.strftime("%Y-%m-%d") if hasattr(exp, "strftime") else str(exp)[:10]
            candidates.append((ts, ymd))
        candidates.sort(key=lambda x: x[1])
        upcoming = [c for c in candidates if c[1] >= today]
        return (upcoming[0] if upcoming else candidates[-1] if candidates else (None, None))[0]
    except Exception:  # noqa: BLE001
        return None


def _compute_nifty_futures_premium(access_token: str, session_ymd: str, spot_close: float) -> dict[str, Any] | None:
    client = _kite_client(access_token)
    if not client:
        return None
    ts = _resolve_nearest_nifty_future(client)
    if not ts:
        return None
    token = _instrument_token(client, f"NFO:{ts}")
    if token is None:
        return None
    from_d, to_d = _ist_range(14)
    try:
        pause_before_kite_historical()
        bars = client.get_historical_data(token, from_d, to_d, "day")
        bars = _bars_through_session(bars, session_ymd)
        picked = completed_session_bars(bars, session_ymd)
        if not picked:
            return None
        fut_close = round2(float(picked["session"]["close"]))
        return {
            "nifty_futures_close": fut_close,
            "premium_discount": round2(fut_close - spot_close),
        }
    except Exception:  # noqa: BLE001
        return None


def fetch_market_enrichment(
    access_token: str | None,
    session_ymd: str,
    use_live_quote: bool = False,
) -> dict[str, Any]:
    computed_from: list[str] = []
    out: dict[str, Any] = {"computed_from": computed_from}

    if session_ymd:
        deriv = _compute_derivatives()
        if deriv:
            out["derivatives"] = deriv
            computed_from.append("NSE option chain (PCR, max pain, OI walls)")

    if not session_ymd:
        return out

    nifty_fact, nifty_src = _resolve_technical_levels(
        access_token, "NSE:NIFTY 50", YAHOO_NIFTY_50, session_ymd, prefer_yahoo=not use_live_quote
    )
    bank_fact, bank_src = _resolve_technical_levels(
        access_token, "NSE:NIFTY BANK", YAHOO_BANK_NIFTY, session_ymd, prefer_yahoo=not use_live_quote
    )

    movers_res = None
    if access_token:
        try:
            movers_res = _compute_top_movers(access_token, session_ymd, use_live_quote)
        except Exception as exc:  # noqa: BLE001
            log.warning("top movers failed: %s", exc)

    if not movers_res:
        try:
            nse_movers, breadth = get_nifty50_movers()
            movers_res = {"movers": nse_movers, "breadth": breadth}
            computed_from.append("NSE Nifty 50 index constituents")
        except Exception:  # noqa: BLE001
            pass

    if movers_res:
        out["top_movers"] = movers_res["movers"]
        out["advance_decline"] = movers_res["breadth"]
        if "NSE Nifty 50" not in " ".join(computed_from):
            computed_from.append("Kite quote (Nifty 50 movers + breadth)")

    tl: dict[str, Any] = {}
    if nifty_fact:
        tl["nifty50"] = nifty_fact
    if bank_fact:
        tl["bank_nifty"] = bank_fact
    if tl:
        out["technical_levels"] = tl
        for src in (nifty_src, bank_src):
            if src and src not in computed_from:
                computed_from.append(src)

    if access_token and nifty_fact and nifty_fact.get("close"):
        fut = _compute_nifty_futures_premium(access_token, session_ymd, nifty_fact["close"])
        if fut:
            out["derivatives"] = {**(out.get("derivatives") or {}), **fut}
            computed_from.append("Kite NFO (Nifty futures close + premium/discount)")

    return out


def format_enrichment_for_prompt(enrich: dict[str, Any]) -> str:
    if not enrich.get("computed_from"):
        return ""
    payload = {
        "top_movers": enrich.get("top_movers"),
        "advance_decline": enrich.get("advance_decline"),
        "technical_levels": enrich.get("technical_levels"),
        "derivatives": enrich.get("derivatives"),
    }
    return f"""COMPUTED INDICATORS (authoritative numbers from {", ".join(enrich["computed_from"])} — copy exactly; these describe the PRIOR closed session unless noted):
{json.dumps(payload, indent=2)}

Use COMPUTED INDICATORS for numbers only. You set overall_bias and outlook from OVERNIGHT SIGNALS — server will not override. trade_setups are computed server-side; set trade_setups to []."""
