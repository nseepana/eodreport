"""NSE India unofficial API client (mirrors kiteob/lib/nse-preopen.ts).

Akamai mitigations:
  - curl_cffi Chrome TLS impersonation when installed (preferred)
  - Accept homepage cookies even on HTTP 403 (bot cookies still authorize /api)
  - Option-chain endpoints need an extra /option-chain navigation hit
  - Single-flight cookie seed + sequential callers (no parallel burst)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

try:
    from curl_cffi import requests as cffi_requests

    _HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None  # type: ignore[misc, assignment]
    _HAS_CURL_CFFI = False

NSE_BASE = "https://www.nseindia.com"
OPTION_CHAIN_PAGE = f"{NSE_BASE}/option-chain"
TIMEOUT = 12
COOKIE_TTL = 300
SEED_ATTEMPTS = 3

_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

_lock = threading.Lock()
_cookie_cache: str | None = None
_cookie_expires = 0.0
_seed_in_flight: threading.Event | None = None
_option_chain_cookie_cache: str | None = None
_option_chain_cookie_expires = 0.0
_option_chain_seed_in_flight: threading.Event | None = None


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)) and v == v:
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").strip())
        except ValueError:
            return None
    return None


def invalidate_cookie() -> None:
    global _cookie_cache, _cookie_expires
    global _option_chain_cookie_cache, _option_chain_cookie_expires
    with _lock:
        _cookie_cache = None
        _cookie_expires = 0.0
        _option_chain_cookie_cache = None
        _option_chain_cookie_expires = 0.0


def _merge_set_cookie_headers(existing: str, response: Any) -> str:
    """Append Set-Cookie values from a response (403 may still carry bot cookies)."""
    parts = [p.strip() for p in existing.split(";") if p.strip()]
    seen = {p.split("=", 1)[0] for p in parts if "=" in p}
    raw_headers = getattr(response, "headers", None)
    if raw_headers is None:
        return existing
    set_cookies: list[str] = []
    if hasattr(raw_headers, "getlist"):
        set_cookies = list(raw_headers.getlist("set-cookie") or [])
    else:
        sc = raw_headers.get("set-cookie") or raw_headers.get("Set-Cookie")
        if sc:
            set_cookies = [sc] if isinstance(sc, str) else list(sc)
    for item in set_cookies:
        pair = item.split(";", 1)[0].strip()
        if not pair or "=" not in pair:
            continue
        name = pair.split("=", 1)[0]
        if name not in seen:
            parts.append(pair)
            seen.add(name)
    return "; ".join(parts)


def _session_cookie_header(session: requests.Session) -> str:
    return "; ".join(f"{k}={v}" for k, v in session.cookies.items())


def _cookies_from_session(session: Any) -> str:
    jar = getattr(session, "cookies", None)
    if jar is None:
        return ""
    try:
        items = jar.items()  # curl_cffi
    except AttributeError:
        items = session.cookies.get_dict().items()  # requests fallback
    return "; ".join(f"{k}={v}" for k, v in items)


def _http_get(
    url: str,
    *,
    headers: dict[str, str],
    cookie: str = "",
    session: Any | None = None,
) -> tuple[int, str, str]:
    """GET → (status_code, body_text, cookie_header)."""
    hdrs = dict(headers)
    if cookie:
        hdrs["Cookie"] = cookie

    if _HAS_CURL_CFFI:
        sess = session or cffi_requests.Session(impersonate="chrome124")
        r = sess.get(url, headers=hdrs, timeout=TIMEOUT)
        merged = _merge_set_cookie_headers(cookie, r)
        jar_cookie = _cookies_from_session(sess)
        if jar_cookie:
            merged = jar_cookie if not merged else f"{merged}; {jar_cookie}" if jar_cookie not in merged else merged
        return r.status_code, r.text, merged

    sess = session if session is not None else requests.Session()
    r = sess.get(url, headers=hdrs, timeout=TIMEOUT)
    merged = _merge_set_cookie_headers(_session_cookie_header(sess) or cookie, r)
    if not merged:
        merged = cookie
    return r.status_code, r.text, merged


def _seed_homepage_cookie(force: bool = False) -> str:
    """Hit NSE homepage; accept cookies even when status is 403."""
    global _cookie_cache, _cookie_expires, _seed_in_flight

    with _lock:
        if not force and _cookie_cache and time.time() < _cookie_expires:
            return _cookie_cache
        if _seed_in_flight is not None and not force:
            waiter = _seed_in_flight
        else:
            waiter = None
            _seed_in_flight = threading.Event()

    if waiter is not None:
        waiter.wait(timeout=TIMEOUT * SEED_ATTEMPTS + 2)
        with _lock:
            if _cookie_cache:
                return _cookie_cache
        force = True

    last_err = "no cookies"
    try:
        for attempt in range(1, SEED_ATTEMPTS + 1):
            status, _body, cookie = _http_get(
                NSE_BASE + "/",
                headers={**_BROWSER_HEADERS, "Referer": NSE_BASE + "/"},
            )
            if cookie:
                with _lock:
                    _cookie_cache = cookie
                    _cookie_expires = time.time() + COOKIE_TTL
                if status == 403:
                    log.debug("nse seed HTTP 403 but cookies received (Akamai bot path)")
                return cookie
            last_err = f"HTTP {status} (no cookies)"
            log.warning("nse seed attempt %s/%s %s", attempt, SEED_ATTEMPTS, last_err)
            time.sleep(0.3 * attempt)
    finally:
        with _lock:
            if _seed_in_flight is not None:
                _seed_in_flight.set()
                _seed_in_flight = None

    raise RuntimeError(f"NSE cookie seed failed: {last_err}")


def _seed_option_chain_cookie(force: bool = False) -> str:
    """Homepage cookies + /option-chain navigation (required for v3 API)."""
    global _option_chain_cookie_cache, _option_chain_cookie_expires, _option_chain_seed_in_flight

    with _lock:
        if not force and _option_chain_cookie_cache and time.time() < _option_chain_cookie_expires:
            return _option_chain_cookie_cache
        if _option_chain_seed_in_flight is not None and not force:
            waiter = _option_chain_seed_in_flight
        else:
            waiter = None
            _option_chain_seed_in_flight = threading.Event()

    if waiter is not None:
        waiter.wait(timeout=TIMEOUT * SEED_ATTEMPTS + 2)
        with _lock:
            if _option_chain_cookie_cache:
                return _option_chain_cookie_cache
        force = True

    try:
        base = _seed_homepage_cookie(force=force)
        status, _body, merged = _http_get(
            OPTION_CHAIN_PAGE,
            headers={
                **_BROWSER_HEADERS,
                "Referer": NSE_BASE + "/",
                "sec-fetch-site": "same-origin",
            },
            cookie=base,
        )
        cookie = merged or base
        if not cookie:
            raise RuntimeError(f"option-chain seed HTTP {status} (no cookies)")
        with _lock:
            _option_chain_cookie_cache = cookie
            _option_chain_cookie_expires = time.time() + COOKIE_TTL
        return cookie
    finally:
        with _lock:
            if _option_chain_seed_in_flight is not None:
                _option_chain_seed_in_flight.set()
                _option_chain_seed_in_flight = None


def _api_headers(*, referer: str) -> dict[str, str]:
    return {
        **_BROWSER_HEADERS,
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def _nse_fetch(path: str, *, option_chain: bool = False) -> Any:
    referer = OPTION_CHAIN_PAGE if option_chain else NSE_BASE + "/"
    cookie_fn = _seed_option_chain_cookie if option_chain else _seed_homepage_cookie

    for retry in range(3):
        force = retry > 0
        if force:
            invalidate_cookie()
        cookie = cookie_fn(force=force)
        status, body, _ = _http_get(
            NSE_BASE + path,
            headers=_api_headers(referer=referer),
            cookie=cookie,
        )
        if status in (401, 403) and retry < 2:
            log.warning("nse %s HTTP %s — reseeding cookies", path, status)
            time.sleep(0.3 * (retry + 1))
            continue
        if status != 200:
            raise RuntimeError(f"NSE {path} HTTP {status}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"NSE {path} invalid JSON") from exc
    raise RuntimeError(f"NSE {path} unreachable")


def nse_get(path: str) -> Any:
    return _nse_fetch(path, option_chain=False)


def nse_option_chain_get(path: str) -> Any:
    return _nse_fetch(path, option_chain=True)


def get_all_indices() -> list[dict[str, Any]]:
    raw = nse_get("/api/allIndices")
    return list(raw.get("data") or [])


def pick_index(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    target = name.strip().upper()
    for row in rows:
        if (row.get("index") or "").strip().upper() == target:
            return row
    return None


def get_indices_snapshot() -> dict[str, Any]:
    rows = get_all_indices()

    def level(name: str) -> dict[str, Any] | None:
        row = pick_index(rows, name)
        if not row:
            return None
        return {
            "name": name,
            "last": _num(row.get("last")),
            "change_pct": _num(row.get("percentChange")),
            "open": _num(row.get("open")),
            "prev_close": _num(row.get("previousClose")),
            "high": _num(row.get("high")),
            "low": _num(row.get("low")),
            "change": _num(row.get("variation")),
        }

    return {
        "nifty50": level("NIFTY 50"),
        "bank_nifty": level("NIFTY BANK"),
        "india_vix": level("INDIA VIX"),
        "sensex": level("S&P BSE SENSEX"),
        "midcap": level("NIFTY MIDCAP 100"),
        "smallcap": level("NIFTY SMLCAP 100"),
        "_all": rows,
    }


def get_fii_dii_flows() -> list[dict[str, Any]]:
    raw = nse_get("/api/fiidiiTradeReact")
    if not isinstance(raw, list):
        return []
    return [
        {
            "category": (row.get("category") or "").strip(),
            "date": row.get("date"),
            "buy_value": _num(row.get("buyValue")),
            "sell_value": _num(row.get("sellValue")),
            "net_value": _num(row.get("netValue")),
        }
        for row in raw
    ]


def get_nifty50_movers() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    raw = nse_get("/api/equity-stockIndices?index=NIFTY%2050")
    stocks = list(raw.get("data") or [])
    rows = []
    for s in stocks:
        pct = _num(s.get("pChange"))
        if pct is None:
            continue
        rows.append({"stock": s.get("symbol", ""), "change_pct": round(pct, 2)})
    rows.sort(key=lambda x: x["change_pct"], reverse=True)
    adv = sum(1 for r in rows if r["change_pct"] > 0.05)
    dec = sum(1 for r in rows if r["change_pct"] < -0.05)
    unch = len(rows) - adv - dec
    ratio = f"{adv / dec:.2f}:1" if dec else (f"{adv}:0" if adv else "0:0")
    breadth = {"advances": adv, "declines": dec, "unchanged": unch, "ratio": ratio}
    gainers = rows[:5]
    losers = list(reversed(rows[-5:])) if len(rows) >= 5 else list(reversed(rows))
    return {"gainers": gainers, "losers": losers}, breadth


def get_option_chain_metrics(symbol: str = "NIFTY") -> dict[str, Any] | None:
    info = nse_option_chain_get(f"/api/option-chain-contract-info?symbol={symbol}")
    expiries = info.get("expiryDates") or []
    if not expiries:
        return None
    expiry = expiries[0]
    enc_expiry = expiry.replace(" ", "%20")
    raw = nse_option_chain_get(
        f"/api/option-chain-v3?type=Indices&symbol={symbol}&expiry={enc_expiry}"
    )
    rows = raw.get("records", {}).get("data") or []
    by_strike: dict[float, tuple[float, float]] = {}
    max_call = (0.0, 0.0)
    max_put = (0.0, 0.0)
    for row in rows:
        if row.get("expiryDates") and row["expiryDates"] != expiry:
            continue
        strike = _num(row.get("strikePrice"))
        if strike is None:
            continue
        ce = _num((row.get("CE") or {}).get("openInterest")) or 0.0
        pe = _num((row.get("PE") or {}).get("openInterest")) or 0.0
        by_strike[strike] = (ce, pe)
        if ce > max_call[1]:
            max_call = (strike, ce)
        if pe > max_put[1]:
            max_put = (strike, pe)
    if not by_strike:
        return None
    filtered = raw.get("filtered") or {}
    tc = _num((filtered.get("CE") or {}).get("totOI"))
    tp = _num((filtered.get("PE") or {}).get("totOI"))
    if tc and tp:
        pcr = tp / tc
    else:
        tc = sum(v[0] for v in by_strike.values())
        tp = sum(v[1] for v in by_strike.values())
        pcr = tp / tc if tc else None
    strikes = sorted(by_strike)
    min_pain = float("inf")
    mp: float | None = None
    for s in strikes:
        pain = 0.0
        for k, (ce, pe) in by_strike.items():
            if s > k:
                pain += (s - k) * ce
            if s < k:
                pain += (k - s) * pe
        if pain < min_pain:
            min_pain = pain
            mp = s
    return {
        "symbol": symbol,
        "expiry": expiry,
        "underlying": _num(raw.get("records", {}).get("underlyingValue")),
        "pcr": round(pcr, 3) if pcr is not None else None,
        "max_pain": mp,
        "key_call_wall": max_call[0] if max_call[1] else None,
        "key_put_wall": max_put[0] if max_put[1] else None,
    }


def get_pre_open_avg_change() -> float | None:
    try:
        raw = nse_get("/api/market-data-pre-open?key=NIFTY")
        stocks = raw.get("data") or []
        vals = [_num(s.get("metadata", {}).get("pChange")) for s in stocks]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 2)
    except Exception:
        return None


def get_gift_nifty() -> dict[str, Any] | None:
    try:
        raw = nse_get("/api/marketStatus")
        g = raw.get("giftnifty") if isinstance(raw, dict) else None
        if not g:
            return None
        return {
            "last": _num(g.get("LASTPRICE")),
            "change": _num(g.get("DAYCHANGE")),
            "change_pct": _num(g.get("PERCHANGE")),
            "expiry": g.get("EXPIRYDATE"),
            "contracts_traded": _num(g.get("CONTRACTSTRADED")),
            "timestamp": g.get("TIMESTMP"),
        }
    except Exception:
        return None


def get_pre_open_snapshot(key: str = "NIFTY") -> dict[str, Any]:
    try:
        raw = nse_get(f"/api/market-data-pre-open?key={key}")
        stocks = []
        for item in raw.get("data") or []:
            md = item.get("metadata") or {}
            pm = (item.get("detail") or {}).get("preOpenMarket") or {}
            symbol = (md.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            iep = _num(pm.get("IEP")) or _num(pm.get("finalPrice")) or _num(md.get("lastPrice"))
            prev_close = _num(md.get("previousClose"))
            change_pct = _num(md.get("pChange"))
            if change_pct is None and iep is not None and prev_close:
                change_pct = ((iep - prev_close) / prev_close) * 100
            buy = _num(pm.get("totalBuyQuantity"))
            sell = _num(pm.get("totalSellQuantity"))
            imbalance = (buy / sell) if buy is not None and sell else None
            stocks.append({
                "symbol": symbol,
                "iep": iep,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "ieq": _num(pm.get("finalQuantity")) or _num(pm.get("totalTradedVolume")),
                "total_buy_qty": buy,
                "total_sell_qty": sell,
                "imbalance": imbalance,
                "last_update_time": pm.get("lastUpdateTime"),
            })
        return {
            "key": key,
            "advances": _num(raw.get("advances")),
            "declines": _num(raw.get("declines")),
            "unchanged": _num(raw.get("unchanged")),
            "stocks": stocks,
        }
    except Exception:
        return {"key": key, "stocks": []}
