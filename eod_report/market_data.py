"""Authoritative live market facts (NSE + yfinance + Kite + overnight signals)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import yfinance as yf

from eod_report.indicators import round2, sector_bias
from eod_report.kite_throttle import pause_before_kite_historical
from eod_report.nse_client import (
    get_fii_dii_flows,
    get_gift_nifty,
    get_indices_snapshot,
    get_pre_open_snapshot,
    invalidate_cookie,
)
from eod_report.session import (
    data_session_for_target,
    is_kite_quote_ohlc_ready,
    is_live_data_session,
    ist_hhmm,
    ist_now,
    last_completed_session_iso,
    resolve_target_session,
)

log = logging.getLogger(__name__)

YAHOO_NIFTY_50 = "^NSEI"
YAHOO_BANK_NIFTY = "^NSEBANK"
YAHOO_SENSEX = "^BSESN"

KITE_INDICES = {
    "nifty50": ["NSE:NIFTY 50"],
    "bank_nifty": ["NSE:NIFTY BANK"],
    "sensex": ["BSE:SENSEX"],
    "nifty_midcap": ["NSE:NIFTY MIDCAP 100", "NSE:NIFTY MIDCAP SELECT"],
    "nifty_smallcap": ["NSE:NIFTY SMLCAP 100", "NSE:NIFTY SMALLCAP 50"],
    "india_vix": ["NSE:INDIA VIX"],
}

KITE_SECTORS = [
    {"sector": label, "keys": [f"NSE:{k}" for k in keys.split(",")]}
    for label, keys in {
        "Nifty Fin Services": "NIFTY FIN SERVICE,NIFTY FINANCIAL SERVICES",
        "Nifty Bank": "NIFTY BANK",
        "Nifty IT": "NIFTY IT",
        "Nifty Auto": "NIFTY AUTO",
        "Nifty Pharma": "NIFTY PHARMA",
        "Nifty FMCG": "NIFTY FMCG",
        "Nifty Metal": "NIFTY METAL",
        "Nifty Realty": "NIFTY REALTY",
        "Nifty Energy": "NIFTY ENERGY",
        "Nifty PSU Bank": "NIFTY PSU BANK",
        "Nifty Private Bank": "NIFTY PVT BANK,NIFTY PRIVATE BANK",
        "Nifty Oil & Gas": "NIFTY OIL AND GAS,NIFTY OIL & GAS",
        "Nifty Healthcare": "NIFTY HEALTHCARE,NIFTY HEALTHCARE INDEX",
        "Nifty Consumer Durables": "NIFTY CONSR DURBL,NIFTY CONSUMER DURABLES",
        "Nifty Media": "NIFTY MEDIA",
    }.items()
]

GLOBAL_SYMBOLS = [
    ("^DJI", "dow", "Dow Jones", "us"),
    ("^GSPC", "sp500", "S&P 500", "us"),
    ("^IXIC", "nasdaq", "Nasdaq", "us"),
    ("^N225", "nikkei", "Nikkei 225", "asia"),
    ("^HSI", "hangseng", "Hang Seng", "asia"),
    ("^KS11", "kospi", "Kospi", "asia"),
    ("^GDAXI", "dax", "DAX", "europe"),
    ("^FTSE", "ftse", "FTSE 100", "europe"),
    ("^FCHI", "cac", "CAC 40", "europe"),
    ("INR=X", "usdinr", "USD/INR", "macro"),
    ("BZ=F", "brent", "Brent crude", "macro"),
    ("CL=F", "wti", "WTI crude", "macro"),
    ("GC=F", "gold", "Gold", "macro"),
    ("SI=F", "silver", "Silver", "macro"),
    ("HG=F", "copper", "Copper", "macro"),
    ("DX-Y.NYB", "dxy", "DXY", "macro"),
    ("NG=F", "natgas", "Nat Gas", "macro"),
    ("^VIX", "usvix", "US VIX", "macro"),
    ("^TNX", "us10y", "US 10Y yield", "macro"),
    ("^TYX", "us20y", "US 20Y yield", "macro"),
    ("TLT", "tlt", "TLT", "macro"),
]

IST_SESSION_OPEN = 915
IST_SESSION_CLOSE = 1530
_token_cache: dict[str, int] = {}


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)) and v == v:
        return float(v)
    return None


def _normalize_key(key: str) -> str:
    return key.strip().upper().replace("  ", " ")


def _kite_client(access_token: str | None):
    if not access_token:
        return None
    from eod_report.kite_client import EodKiteClient

    return EodKiteClient(access_token)


def _parse_kite_ohlc_row(row: dict[str, Any] | None) -> dict[str, float] | None:
    if not row:
        return None
    ohlc = row.get("ohlc") or {}
    day_open = _num(ohlc.get("open"))
    day_high = _num(ohlc.get("high"))
    day_low = _num(ohlc.get("low"))
    prev_close = _num(ohlc.get("close"))
    last_price = _num(row.get("last_price"))
    if None in (day_open, day_high, day_low, prev_close, last_price):
        return None
    if day_open == 0 and day_high == 0 and day_low == 0 and prev_close > 500:
        return {
            "day_open": last_price,
            "day_high": last_price,
            "day_low": last_price,
            "prev_close": prev_close,
            "last_price": last_price,
        }
    return {
        "day_open": day_open,
        "day_high": day_high,
        "day_low": day_low,
        "prev_close": prev_close,
        "last_price": last_price,
    }


def fetch_kite_session_ohlc_map(access_token: str, keys: list[str]) -> dict[str, dict[str, float]]:
    client = _kite_client(access_token)
    if not client:
        return {}
    uniq = list(dict.fromkeys(_normalize_key(k) for k in keys))
    out: dict[str, dict[str, float]] = {}
    chunk = 100
    for i in range(0, len(uniq), chunk):
        batch = uniq[i : i + chunk]
        try:
            data = client.get_ohlc(batch)
        except Exception as exc:  # noqa: BLE001
            log.warning("kite ohlc failed: %s", exc)
            continue
        for key, val in data.items():
            parsed = _parse_kite_ohlc_row(val)
            if parsed:
                out[key] = parsed
    return out


def _instrument_token(client, key: str) -> int | None:
    norm = _normalize_key(key)
    if norm in _token_cache:
        return _token_cache[norm]
    ex, sym = norm.split(":", 1)
    try:
        for row in client.instruments(ex):
            ts = f"{row['exchange']}:{row['tradingsymbol']}".upper()
            if ts == norm or row.get("tradingsymbol", "").upper() == sym:
                _token_cache[norm] = int(row["instrument_token"])
                return _token_cache[norm]
    except Exception as exc:  # noqa: BLE001
        log.warning("instrument lookup failed %s: %s", key, exc)
    return None


def _ist_range(days: int) -> tuple[str, str]:
    now = ist_now()
    to_d = now.date()
    from_d = to_d - timedelta(days=days)
    return from_d.isoformat(), to_d.isoformat()


def _bar_ymd(bar: dict[str, Any]) -> str:
    t = bar.get("date")
    if hasattr(t, "strftime"):
        return t.strftime("%Y-%m-%d")
    return str(t)[:10]


def completed_session_bars(bars: list[dict[str, Any]], session_ymd: str) -> dict[str, Any] | None:
    if not bars:
        return None
    idx = -1
    for i in range(len(bars) - 1, -1, -1):
        if _bar_ymd(bars[i]) == session_ymd:
            idx = i
            break
    if idx < 0:
        return None
    return {
        "session": bars[idx],
        "prev": bars[idx - 1] if idx > 0 else None,
        "session_index": idx,
    }


def index_fact_from_bars(bars: list[dict[str, Any]], session_ymd: str) -> dict[str, Any] | None:
    picked = completed_session_bars(bars, session_ymd)
    if not picked:
        return None
    bar = picked["session"]
    prev = picked.get("prev")
    prev_close = _num((prev or {}).get("close"))
    close = _num(bar.get("close"))
    if close is None:
        return None
    change = close - prev_close if prev_close is not None else None
    change_pct = ((change / prev_close) * 100) if change is not None and prev_close else None
    return {
        "open": round2(_num(bar.get("open")) or 0),
        "high": round2(_num(bar.get("high")) or 0),
        "low": round2(_num(bar.get("low")) or 0),
        "close": round2(close),
        "change": round2(change) if change is not None else None,
        "change_pct": round2(change_pct) if change_pct is not None else None,
    }


def fetch_historical_index_facts(
    access_token: str,
    keys: list[str],
    session_ymd: str,
) -> dict[str, dict[str, Any]]:
    client = _kite_client(access_token)
    if not client:
        return {}
    from_d, to_d = _ist_range(14)
    out: dict[str, dict[str, Any]] = {}

    def task(key: str) -> tuple[str, dict[str, Any] | None] | None:
        token = _instrument_token(client, key)
        if token is None:
            return None
        try:
            pause_before_kite_historical()
            bars = client.get_historical_data(token, from_d, to_d, "day")
            fact = index_fact_from_bars(bars, session_ymd)
            return (key, fact) if fact else None
        except Exception:  # noqa: BLE001
            return None

    for key in keys:
        row = task(key)
        if row:
            out[row[0]] = row[1]
    return out


def kite_to_index_fact(row: dict[str, float]) -> dict[str, Any]:
    change = row["last_price"] - row["prev_close"]
    change_pct = (change / row["prev_close"]) * 100 if row["prev_close"] else 0
    return {
        "open": round2(row["day_open"]),
        "high": round2(row["day_high"]),
        "low": round2(row["day_low"]),
        "close": round2(row["last_price"]),
        "change": round2(change),
        "change_pct": round2(change_pct),
    }


def fetch_yahoo_index_fact(symbol: str, session_ymd: str) -> dict[str, Any] | None:
    try:
        end = datetime.strptime(session_ymd, "%Y-%m-%d") + timedelta(days=1)
        start = end - timedelta(days=14)
        hist = yf.Ticker(symbol).history(start=start, end=end, interval="1d")
        if hist.empty:
            return None
        hist = hist.reset_index()
        bars = []
        for _, row in hist.iterrows():
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
        return index_fact_from_bars(bars, session_ymd)
    except Exception as exc:  # noqa: BLE001
        log.debug("yahoo index %s failed: %s", symbol, exc)
        return None


def fetch_global_cues() -> dict[str, Any]:
    macro: dict[str, Any] = {}
    by_key: dict[str, dict[str, Any]] = {}
    for symbol, key, _label, _region in GLOBAL_SYMBOLS:
        try:
            q = yf.Ticker(symbol).fast_info
            price = _num(getattr(q, "last_price", None) or getattr(q, "lastPrice", None))
            change_pct = _num(getattr(q, "year_change", None))
            info = yf.Ticker(symbol).info
            if price is None:
                price = _num(info.get("regularMarketPrice"))
            if change_pct is None:
                change_pct = _num(info.get("regularMarketChangePercent"))
            by_key[key] = {"price": price, "change_pct": change_pct}
        except Exception:  # noqa: BLE001
            try:
                info = yf.Ticker(symbol).info
                by_key[key] = {
                    "price": _num(info.get("regularMarketPrice")),
                    "change_pct": _num(info.get("regularMarketChangePercent")),
                }
            except Exception:  # noqa: BLE001
                by_key[key] = {}

    def pct_entry(label: str, k: str) -> dict[str, Any]:
        cp = by_key.get(k, {}).get("change_pct")
        return {"label": label, "change_pct": round2(cp) if cp is not None else None}

    macro = {
        "usd_inr": by_key.get("usdinr", {}).get("price"),
        "brent_crude_usd": by_key.get("brent", {}).get("price"),
        "wti_crude_usd": by_key.get("wti", {}).get("price"),
        "gold_usd": by_key.get("gold", {}).get("price"),
        "silver_usd": by_key.get("silver", {}).get("price"),
        "copper_usd": by_key.get("copper", {}).get("price"),
        "natgas_usd": by_key.get("natgas", {}).get("price"),
        "us_vix": by_key.get("usvix", {}).get("price"),
        "dxy": by_key.get("dxy", {}).get("price"),
        "us_10yr_yield_pct": by_key.get("us10y", {}).get("price"),
        "us_20yr_yield_pct": by_key.get("us20y", {}).get("price"),
        "tlt_pct": round2(by_key["tlt"]["change_pct"]) if by_key.get("tlt", {}).get("change_pct") is not None else None,
    }
    global_cues = {
        "us_markets": {
            "dow_pct": round2(by_key["dow"]["change_pct"]) if by_key.get("dow", {}).get("change_pct") is not None else None,
            "sp500_pct": round2(by_key["sp500"]["change_pct"]) if by_key.get("sp500", {}).get("change_pct") is not None else None,
            "nasdaq_pct": round2(by_key["nasdaq"]["change_pct"]) if by_key.get("nasdaq", {}).get("change_pct") is not None else None,
        },
        "asia": [pct_entry("Nikkei 225", "nikkei"), pct_entry("Hang Seng", "hangseng"), pct_entry("Kospi", "kospi")],
        "europe": [pct_entry("DAX", "dax"), pct_entry("FTSE 100", "ftse"), pct_entry("CAC 40", "cac")],
    }
    return {"macro": macro, "global_cues": global_cues}


def flows_to_fii_dii(flows: list[dict[str, Any]]) -> dict[str, Any]:
    fii = next((f for f in flows if "fii" in f.get("category", "").lower() or "fpi" in f.get("category", "").lower()), None)
    dii = next((f for f in flows if "dii" in f.get("category", "").lower()), None)
    return {
        "fii_cash_net_cr": fii.get("net_value") if fii else None,
        "dii_cash_net_cr": dii.get("net_value") if dii else None,
        "flow_date": (fii or dii or {}).get("date"),
    }


def _india_vix_context(vix: float | None) -> str | None:
    if vix is None:
        return None
    if vix >= 18:
        return "Elevated — expect wider intraday swings and gap extension risk"
    if vix >= 14:
        return "Normal — standard gap-follow-through rules apply"
    return "Compressed — gap direction still driven by GIFT/US; low VIX is not a gap-up signal"


def _compute_gap_read(
    session_change: dict[str, Any],
    us_sp500_pct: float | None,
    gift_change_pct: float | None,
    india_vix: float | None,
) -> dict[str, str]:
    parts: list[str] = []
    score = 0.0
    pct = session_change.get("change_pct")
    src = session_change.get("source")
    has_indian = (pct is not None and src == "gift_nifty") or gift_change_pct is not None

    if pct is not None:
        if src == "gift_nifty":
            parts.append(f"GIFT Nifty {pct:+.2f}%")
        else:
            parts.append(f"Nifty {pct:+.2f}% vs prior close ({src or 'nse'})")
        if pct >= 0.3:
            score += 1
        elif pct <= -0.3:
            score -= 1
    elif gift_change_pct is not None:
        parts.append(f"GIFT Nifty {gift_change_pct:+.2f}%")
        if gift_change_pct >= 0.3:
            score += 1
        elif gift_change_pct <= -0.3:
            score -= 1

    if india_vix is not None:
        ctx = _india_vix_context(india_vix)
        parts.append(f"India VIX {india_vix:.2f}" + (f" — {ctx}" if ctx else ""))

    if us_sp500_pct is not None:
        label = "US S&P (global cue)" if has_indian else "US S&P"
        parts.append(f"{label} {us_sp500_pct:+.2f}%")
        weight = 0.5 if has_indian else 1.0
        if us_sp500_pct <= -1:
            score -= weight
        elif us_sp500_pct <= -0.5:
            score -= 0.5 * weight
        elif us_sp500_pct >= 0.5:
            score += 0.5 * weight

    detail = "; ".join(parts) if parts else "Insufficient overnight data"
    if score >= 0.5:
        gap = "gap_up"
    elif score <= -0.5:
        gap = "gap_down"
    else:
        gap = "flat"
    return {"gap_read": gap, "gap_read_detail": detail}


def _average_preopen_change(stocks: list[dict[str, Any]]) -> float | None:
    vals = [s["change_pct"] for s in stocks if s.get("change_pct") is not None]
    if not vals:
        return None
    return round2(sum(vals) / len(vals))


def _resolve_nifty_session_change(
    indices: dict[str, Any],
    pre_open: dict[str, Any],
    gift: dict[str, Any] | None,
) -> dict[str, Any]:
    hhmm = ist_hhmm()
    in_preopen = 900 <= hhmm < IST_SESSION_OPEN
    overnight = hhmm >= IST_SESSION_CLOSE or hhmm < IST_SESSION_OPEN

    nifty = indices.get("nifty50") or {}
    last = _num(nifty.get("last"))
    prev = _num(nifty.get("prev_close"))
    spot_pct = ((last - prev) / prev * 100) if last is not None and prev else None
    pre_avg = _average_preopen_change(pre_open.get("stocks") or [])
    pre_pct = spot_pct if spot_pct is not None else pre_avg
    gift_pct = _num((gift or {}).get("change_pct"))

    if in_preopen and pre_pct is not None:
        return {"change_pct": round2(pre_pct), "source": "pre_open" if spot_pct is not None else "pre_open_avg"}
    if not overnight and spot_pct is not None:
        return {"change_pct": round2(spot_pct), "source": "nse_spot"}
    if overnight and gift_pct is not None:
        return {"change_pct": round2(gift_pct), "source": "gift_nifty"}
    if overnight and pre_avg is not None:
        return {"change_pct": round2(pre_avg), "source": "pre_open_avg"}
    if spot_pct is not None:
        return {"change_pct": round2(spot_pct), "source": "nse_spot"}
    if pre_avg is not None:
        return {"change_pct": round2(pre_avg), "source": "pre_open_avg"}
    return {}


def fetch_overnight_market_data() -> dict[str, Any]:
    try:
        indices = get_indices_snapshot()
        gift = get_gift_nifty()
        pre_open = get_pre_open_snapshot("NIFTY")
        if not gift and not (indices.get("india_vix") or {}).get("last"):
            invalidate_cookie()
            indices = get_indices_snapshot()
            gift = get_gift_nifty()
            pre_open = get_pre_open_snapshot("NIFTY")
    except Exception as exc:  # noqa: BLE001
        log.warning("overnight fetch failed: %s", exc)
        indices, gift, pre_open = {}, None, {"stocks": []}

    session_change = _resolve_nifty_session_change(indices, pre_open, gift)
    gift_live = False
    if gift and gift.get("timestamp"):
        gift_live = True  # simplified — NSE timestamp present
    return {
        "session_change": session_change,
        "gift": gift,
        "gift_live": gift_live,
        "indices": indices,
        "pre_open": pre_open,
    }


def has_core_indian_market_data(facts: dict[str, Any]) -> bool:
    indices = facts.get("indices") or {}
    n = (indices.get("nifty50") or {}).get("close")
    b = (indices.get("bank_nifty") or {}).get("close")
    return (isinstance(n, (int, float)) and n > 500) or (isinstance(b, (int, float)) and b > 500)


def fetch_live_market_facts(access_token: str | None, report_date: str = "today") -> dict[str, Any]:
    target = resolve_target_session(report_date)
    session_date = data_session_for_target(target)
    live = is_live_data_session(session_date)
    sources: list[str] = []
    notes: list[str] = []

    quote_keys = [k for keys in KITE_INDICES.values() for k in keys]
    quote_keys += [k for s in KITE_SECTORS for k in s["keys"]]

    try:
        flows = get_fii_dii_flows()
        if not flows:
            invalidate_cookie()
            flows = get_fii_dii_flows()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"FII/DII flows fetch failed: {exc}")
        flows = []

    quote_ready = is_kite_quote_ohlc_ready()
    use_kite_quote = live and access_token and quote_ready
    prefer_completed_session_bars = not is_live_data_session(session_date)

    kite_map: dict[str, dict[str, float]] = {}
    if use_kite_quote and access_token:
        kite_map = fetch_kite_session_ohlc_map(access_token, quote_keys)

    try:
        nse_indices = get_indices_snapshot()
    except Exception:  # noqa: BLE001
        nse_indices = {}

    global_bundle = fetch_global_cues()
    overnight = fetch_overnight_market_data()

    hist_map: dict[str, dict[str, Any]] = {}
    if access_token:
        hist_map = fetch_historical_index_facts(access_token, quote_keys, session_date)
        if not use_kite_quote and live:
            notes.append("Before 09:15 IST — using Kite historical daily bars for index session data.")
        elif prefer_completed_session_bars and hist_map:
            notes.append(
                f"Mid-session — index OHLC from Kite historical daily bars for completed session {session_date} "
                "(not intraday quote)."
            )

    if nse_indices.get("nifty50") or nse_indices.get("bank_nifty"):
        sources.append("NSE")
    if overnight.get("gift"):
        sources.append("NSE GIFT Nifty")
    sources.append("Yahoo Finance")

    indices: dict[str, Any] = {}

    def first_ohlc(keys: list[str]) -> dict[str, float] | None:
        for k in keys:
            if k in kite_map:
                return kite_map[k]
        return None

    def hist_fact(keys: list[str]) -> dict[str, Any] | None:
        for k in keys:
            if k in hist_map:
                return hist_map[k]
        return None

    def nse_fact(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        last = _num(row.get("last"))
        if last is None:
            return None
        prev = _num(row.get("prev_close"))
        cp = _num(row.get("change_pct"))
        change = (cp / 100 * prev) if cp is not None and prev else (last - prev if prev else None)
        return {"open": row.get("open"), "close": last, "change": change, "change_pct": cp}

    kn = first_ohlc(KITE_INDICES["nifty50"])
    kb = first_ohlc(KITE_INDICES["bank_nifty"])
    hn = hist_fact(KITE_INDICES["nifty50"])
    hb = hist_fact(KITE_INDICES["bank_nifty"])
    nse_ok = quote_ready and not prefer_completed_session_bars

    if prefer_completed_session_bars and hn:
        indices["nifty50"] = hn
    elif kn:
        indices["nifty50"] = kite_to_index_fact(kn)
    elif hn:
        indices["nifty50"] = hn
    elif nse_ok and nse_indices.get("nifty50"):
        f = nse_fact(nse_indices["nifty50"])
        if f:
            indices["nifty50"] = f

    if prefer_completed_session_bars and hb:
        indices["bank_nifty"] = hb
    elif kb:
        indices["bank_nifty"] = kite_to_index_fact(kb)
    elif hb:
        indices["bank_nifty"] = hb
    elif nse_ok and nse_indices.get("bank_nifty"):
        f = nse_fact(nse_indices["bank_nifty"])
        if f:
            indices["bank_nifty"] = f

    hs = hist_fact(KITE_INDICES["sensex"])
    ks = first_ohlc(KITE_INDICES["sensex"])
    if prefer_completed_session_bars and hs:
        indices["sensex"] = hs
    elif ks:
        indices["sensex"] = kite_to_index_fact(ks)
    elif hs:
        indices["sensex"] = hs

    # Keep the FULL fact, same as nifty50/bank_nifty/sensex above. These two used
    # to be narrowed to {"change_pct": ...}, which discarded the OHLC that
    # hist_fact/first_ohlc had already fetched — Kite serves complete bars for
    # both (MIDCAP 100 and SMLCAP 100). kiteob's Index recap renders a shared
    # Open/High/Low/Close table across all five rows, so the narrowed pair showed
    # four em-dashes each and read as missing data.
    for key, ik in (("nifty_midcap", KITE_INDICES["nifty_midcap"]), ("nifty_smallcap", KITE_INDICES["nifty_smallcap"])):
        h = hist_fact(ik)
        row = first_ohlc(ik)
        if prefer_completed_session_bars and h and h.get("change_pct") is not None:
            indices[key] = h
        elif row:
            fact = kite_to_index_fact(row)
            if fact.get("change_pct") is not None:
                indices[key] = fact
        elif h and h.get("change_pct") is not None:
            indices[key] = h

    if not has_core_indian_market_data({"indices": indices}):
        if fetch_yahoo_index_fact(YAHOO_NIFTY_50, session_date):
            indices["nifty50"] = fetch_yahoo_index_fact(YAHOO_NIFTY_50, session_date)
            notes.append("Index OHLC backfilled from Yahoo Finance (^NSEI).")
        if fetch_yahoo_index_fact(YAHOO_BANK_NIFTY, session_date):
            indices["bank_nifty"] = fetch_yahoo_index_fact(YAHOO_BANK_NIFTY, session_date)

    if access_token and (kite_map or hist_map):
        sources.append("Kite Connect")

    sector_heatmap: list[dict[str, Any]] = []
    for sec in KITE_SECTORS:
        h = hist_fact(sec["keys"])
        row = first_ohlc(sec["keys"])
        if prefer_completed_session_bars and h and h.get("change_pct") is not None:
            pct = h["change_pct"]
        elif row:
            pct = kite_to_index_fact(row).get("change_pct") or 0
        elif h and h.get("change_pct") is not None:
            pct = h["change_pct"]
        else:
            continue
        sector_heatmap.append({
            "sector": sec["sector"],
            "change_pct": round2(pct),
            "bias": sector_bias(pct),
        })
    sector_heatmap.sort(key=lambda x: x["change_pct"], reverse=True)

    vix_nse = (nse_indices.get("india_vix") or {}).get("last")
    vix_kite = first_ohlc(KITE_INDICES["india_vix"])
    india_vix = vix_nse or (round2(vix_kite["last_price"]) if vix_kite else None)

    macro = {**global_bundle["macro"], "india_vix": india_vix}
    fii_dii = flows_to_fii_dii(flows)
    us_sp = (global_bundle["global_cues"].get("us_markets") or {}).get("sp500_pct")

    gift = overnight.get("gift")
    session_change = overnight.get("session_change") or {}
    gap = _compute_gap_read(session_change, us_sp, _num((gift or {}).get("change_pct")), india_vix)

    overnight_signals: dict[str, Any] = {
        "india_vix": india_vix,
        "india_vix_context": _india_vix_context(india_vix),
        **gap,
    }
    if gift:
        overnight_signals["gift_nifty"] = {
            "last": gift.get("last"),
            "change_pct": gift.get("change_pct"),
            "live": overnight.get("gift_live", False),
            "timestamp": gift.get("timestamp"),
        }
    if session_change.get("change_pct") is not None:
        overnight_signals["nifty_session_change_pct"] = session_change["change_pct"]
        overnight_signals["nifty_session_change_source"] = session_change.get("source")

    preopen_index = {
        "nifty50": _num((gift or {}).get("last")) or _num((nse_indices.get("nifty50") or {}).get("last")),
        "bank_nifty": _num((nse_indices.get("bank_nifty") or {}).get("last")),
    }

    notes.append(
        f"Report targets session {target}, analyzed from prior session {session_date} (trading day rolls at 09:15 IST)."
    )

    return {
        "session_date": session_date,
        "next_session_date": target,
        "report_date": target,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "live": live,
        "sources": list(dict.fromkeys(sources)),
        "indices": indices,
        "sector_heatmap": sector_heatmap or None,
        "macro": macro,
        "fii_dii": fii_dii,
        "global_cues": global_bundle["global_cues"],
        "overnight_signals": overnight_signals,
        "preopen_index": preopen_index,
        "notes": notes or None,
    }


def format_facts_for_prompt(facts: dict[str, Any]) -> str:
    overnight = facts.get("overnight_signals")
    session_date = facts.get("session_date", "prior session")
    report_date = facts.get("report_date") or facts.get("next_session_date", "target session")
    rest = {k: v for k, v in facts.items() if k not in ("overnight_signals",)}
    block = f"""TARGET SESSION (tomorrow's plan — primary narrative focus): {report_date}
PRIOR SESSION (yesterday — context only; do NOT make this the headline): {session_date}

YESTERDAY DATA — copy numbers exactly into indices/sector/macro/fii fields; keep narrative forward-looking:
{json.dumps(rest, indent=2)}"""
    if overnight:
        block += f"""

TOMORROW OPEN SIGNALS (authoritative — bias & analyst_note MUST align with gap_read):
{json.dumps(overnight, indent=2)}
Rules: gap_read=gap_down → bias neutral/cautiously-bearish/bearish only. session_theme/open_view must lead with expected OPEN, not yesterday's sector leaders."""
    return block
