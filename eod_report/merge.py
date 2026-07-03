"""Merge authoritative live facts and enrichment over LLM output."""

from __future__ import annotations

import re
from typing import Any

from eod_report.finalize import finalize_eod_report


def round_pct(n: Any) -> float | None:
    if isinstance(n, (int, float)) and n == n:
        return round(float(n) * 100) / 100
    return None


def strip_citation_artifacts(text: str) -> str:
    out = re.sub(r"\[\d+\]", "", text)
    out = re.sub(r"\s+([.,;:])", r"\1", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip()


def is_index_field_placeholder(
    field: str,
    value: Any,
    close: float | None = None,
    ctx: dict[str, Any] | None = None,
) -> bool:
    if value is None:
        return True
    if not isinstance(value, (int, float)) or value != value:
        return True
    ctx = ctx or {}
    if field in ("open", "high", "low"):
        if value == 0:
            return True
        if close is not None and close > 500 and value < close * 0.05:
            return True
    if field in ("change_pct", "change"):
        if value == 0:
            ch = ctx.get("change")
            chp = ctx.get("change_pct")
            o = ctx.get("open")
            c = close if close is not None else ctx.get("close")
            if field == "change_pct" and isinstance(ch, (int, float)) and abs(ch) > 0.05:
                return True
            if field == "change" and isinstance(chp, (int, float)) and abs(chp) > 0.005:
                return True
            if isinstance(o, (int, float)) and isinstance(c, (int, float)) and o > 500:
                if abs(c - o) / o > 0.001:
                    return True
    return False


def merge_index_fact(
    model: dict[str, Any] | None,
    fact: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not fact and not model:
        return None
    m = dict(model or {})
    f = dict(fact or {})
    close = None
    if isinstance(f.get("close"), (int, float)) and not is_index_field_placeholder("close", f["close"]):
        close = float(f["close"])
    elif isinstance(m.get("close"), (int, float)):
        close = float(m["close"])

    def pick(field: str) -> float | None:
        fv, mv = f.get(field), m.get(field)
        ctx = {
            "open": f.get("open") or m.get("open"),
            "change": f.get("change") or m.get("change"),
            "change_pct": f.get("change_pct") or m.get("change_pct"),
            "close": close,
        }
        if fv is not None and not is_index_field_placeholder(field, fv, close, ctx):
            return float(fv)
        if mv is not None and not is_index_field_placeholder(field, mv, close, ctx):
            return float(mv)
        if isinstance(fv, (int, float)):
            return float(fv)
        if isinstance(mv, (int, float)):
            return float(mv)
        return None

    merged = {
        "open": pick("open"),
        "high": pick("high"),
        "low": pick("low"),
        "close": pick("close"),
        "change": pick("change"),
        "change_pct": pick("change_pct"),
    }
    if any(v is not None for v in merged.values()):
        return {k: v for k, v in merged.items() if v is not None}
    return None


def apply_live_facts(report: dict[str, Any], facts: dict[str, Any] | None) -> dict[str, Any]:
    if not facts:
        return report
    out = dict(report)
    indices_facts = facts.get("indices")
    if indices_facts:
        prev = dict(out.get("indices") or {})
        merged: dict[str, Any] = dict(prev)
        for key in ("nifty50", "bank_nifty", "sensex"):
            row = merge_index_fact(prev.get(key), indices_facts.get(key))
            if row:
                merged[key] = row
        for key in ("nifty_midcap", "nifty_smallcap"):
            if indices_facts.get(key):
                merged[key] = {**(prev.get(key) or {}), **indices_facts[key]}
        out["indices"] = merged

    if facts.get("live"):
        out["sector_heatmap"] = facts.get("sector_heatmap") or []
    elif facts.get("sector_heatmap"):
        out["sector_heatmap"] = facts["sector_heatmap"]

    if facts.get("macro"):
        out["macro"] = {**(out.get("macro") or {}), **facts["macro"]}

    if facts.get("fii_dii"):
        prev = dict(out.get("fii_dii") or {})
        fd = facts["fii_dii"]
        out["fii_dii"] = {
            **prev,
            "fii_cash_net_cr": fd.get("fii_cash_net_cr", prev.get("fii_cash_net_cr")),
            "dii_cash_net_cr": fd.get("dii_cash_net_cr", prev.get("dii_cash_net_cr")),
            "flow_date": fd.get("flow_date", prev.get("flow_date")),
        }

    if facts.get("global_cues"):
        out["global_cues"] = {**(out.get("global_cues") or {}), **facts["global_cues"]}

    if facts.get("overnight_signals"):
        out["overnight_signals"] = facts["overnight_signals"]

    if facts.get("report_date"):
        out["report_date"] = facts["report_date"]
    out["session_date"] = facts.get("session_date", out.get("session_date"))
    out["next_session_date"] = facts.get("next_session_date", out.get("next_session_date"))
    if facts.get("fetched_at"):
        out["generated_at"] = facts["fetched_at"]
    return out


def _event_to_string(e: Any) -> str | None:
    if isinstance(e, str):
        return e.strip() or None
    if isinstance(e, dict):
        label = e.get("event") or e.get("title") or e.get("name") or ""
        date = f" ({e['date']})" if e.get("date") else ""
        impact = f" — {e['impact']}" if e.get("impact") else ""
        out = f"{label}{date}{impact}".strip()
        return out or None
    return None


def _coalesce_outlook_fields(report: dict[str, Any]) -> dict[str, Any]:
    existing = dict(report.get("outlook_tomorrow") or {})
    ms = report.get("market_summary") or {}
    top_bias = report.get("bias") or ms.get("overall_bias")
    if not existing.get("bias") and top_bias:
        existing["bias"] = top_bias
    for key in ("key_levels_to_watch", "risks", "catalysts"):
        if not existing.get(key) and isinstance(report.get(key), list):
            existing[key] = report[key]
    if existing:
        report["outlook_tomorrow"] = existing
    return report


def _coalesce_market_summary(report: dict[str, Any]) -> dict[str, Any]:
    existing = dict(report.get("market_summary") or {})
    outlook = report.get("outlook_tomorrow") or {}
    catalysts = outlook.get("catalysts") or report.get("catalysts")
    if not existing.get("overall_bias"):
        existing["overall_bias"] = report.get("bias") or outlook.get("bias")
    if not existing.get("key_catalyst"):
        if isinstance(report.get("key_catalyst"), str):
            existing["key_catalyst"] = report["key_catalyst"]
        elif isinstance(catalysts, list) and catalysts:
            existing["key_catalyst"] = catalysts[0]
    if not existing.get("session_theme") and isinstance(report.get("session_theme"), str):
        existing["session_theme"] = report["session_theme"]
    if existing:
        report["market_summary"] = existing
    return report


def normalize_report(report: dict[str, Any]) -> dict[str, Any]:
    out = _coalesce_market_summary(_coalesce_outlook_fields(dict(report)))

    movers = out.get("top_movers")
    if isinstance(movers, dict):
        for side in ("gainers", "losers"):
            rows = movers.get(side)
            if isinstance(rows, list):
                movers[side] = [
                    {**r, "reason": strip_citation_artifacts(r["reason"])}
                    if isinstance(r, dict) and isinstance(r.get("reason"), str)
                    else r
                    for r in rows
                ]

    gc = out.get("global_cues")
    if isinstance(gc, dict) and isinstance(gc.get("us_markets"), dict):
        us = dict(gc["us_markets"])
        for k in ("dow_pct", "sp500_pct", "nasdaq_pct"):
            if k in us:
                us[k] = round_pct(us[k]) if round_pct(us[k]) is not None else us[k]
        gc["us_markets"] = us

    ms = out.get("market_summary")
    if isinstance(ms, dict):
        for k in ("session_theme", "key_catalyst"):
            if isinstance(ms.get(k), str):
                ms[k] = strip_citation_artifacts(ms[k])

    fd = out.get("fii_dii")
    if isinstance(fd, dict):
        for k in ("fii_stance", "flow_summary"):
            if isinstance(fd.get(k), str):
                fd[k] = strip_citation_artifacts(fd[k])

    if isinstance(out.get("analyst_note"), str):
        out["analyst_note"] = strip_citation_artifacts(out["analyst_note"])

    if isinstance(out.get("upcoming_events"), list):
        out["upcoming_events"] = [
            strip_citation_artifacts(s)
            for s in (_event_to_string(e) for e in out["upcoming_events"])
            if s
        ]

    outlook = out.get("outlook_tomorrow")
    if isinstance(outlook, dict):
        o = dict(outlook)
        for key in ("key_levels_to_watch", "risks", "catalysts"):
            if isinstance(o.get(key), list):
                cleaned = []
                for v in o[key]:
                    if isinstance(v, str):
                        cleaned.append(strip_citation_artifacts(v))
                    elif isinstance(v, (int, float)):
                        cleaned.append(v)
                    else:
                        s = _event_to_string(v)
                        if s:
                            cleaned.append(strip_citation_artifacts(s))
                o[key] = cleaned
        out["outlook_tomorrow"] = o
    return out


def _is_placeholder_reason(reason: str | None) -> bool:
    if not reason or not reason.strip():
        return True
    r = reason.lower()
    return (
        "verified from kite" in r
        or "specific news reason unavailable" in r
        or "news reason unavailable" in r
        or ("search for" in r and "to complete this reason" in r)
    )


def _pick_mover_reason(
    llm: dict[str, Any],
    stock: str,
    side: str,
    session_date: str | None,
) -> str:
    norm = re.sub(r"[^a-z0-9]", "", stock, flags=re.IGNORECASE).upper()

    def find(lst: list[Any] | None) -> str | None:
        if not lst:
            return None
        for row in lst:
            if not isinstance(row, dict):
                continue
            s = re.sub(r"[^a-z0-9]", "", str(row.get("stock", "")), flags=re.IGNORECASE).upper()
            if s == norm and isinstance(row.get("reason"), str):
                return row["reason"]
        return None

    primary = find(llm.get(side))
    alt_side = "losers" if side == "gainers" else "gainers"
    alt = find(llm.get(alt_side))
    if primary and not _is_placeholder_reason(primary):
        return primary.strip()
    if alt and not _is_placeholder_reason(alt):
        return alt.strip()
    date = session_date or "the session"
    tone = "session leadership" if side == "gainers" else "session weakness"
    return f"{stock} among Nifty 50 {tone} on {date}; no verified stock-specific catalyst."


def apply_enrichment(
    report: dict[str, Any],
    enrich: dict[str, Any] | None,
    live_quote: dict[str, float] | None = None,
) -> dict[str, Any]:
    if not enrich or not enrich.get("computed_from"):
        return finalize_eod_report(report, enrich, live_quote)
    out = dict(report)

    movers = enrich.get("top_movers")
    if movers:
        llm = dict(out.get("top_movers") or {})
        session_date = out.get("session_date") if isinstance(out.get("session_date"), str) else None
        out["top_movers"] = {
            "gainers": [
                {**m, "reason": _pick_mover_reason(llm, m["stock"], "gainers", session_date)}
                for m in movers.get("gainers", [])
            ],
            "losers": [
                {**m, "reason": _pick_mover_reason(llm, m["stock"], "losers", session_date)}
                for m in movers.get("losers", [])
            ],
        }

    ad = enrich.get("advance_decline")
    if ad and isinstance(ad, dict) and ad.get("ratio"):
        indices = dict(out.get("indices") or {})
        indices["advance_decline"] = ad["ratio"]
        out["indices"] = indices

    deriv = enrich.get("derivatives")
    if deriv:
        prev = dict(out.get("derivatives") or {})
        out["derivatives"] = {
            **prev,
            "pcr": deriv.get("pcr", prev.get("pcr")),
            "max_pain": deriv.get("max_pain", prev.get("max_pain")),
            "key_call_wall": deriv.get("key_call_wall", prev.get("key_call_wall")),
            "key_put_wall": deriv.get("key_put_wall", prev.get("key_put_wall")),
            "oi_trend": deriv.get("oi_trend", prev.get("oi_trend")),
            "oi_expiry": deriv.get("oi_expiry", prev.get("oi_expiry")),
            "nifty_futures_close": deriv.get("nifty_futures_close", prev.get("nifty_futures_close")),
            "premium_discount": deriv.get("premium_discount", prev.get("premium_discount")),
        }

    tl = enrich.get("technical_levels")
    if tl:
        llm_tl = dict(out.get("technical_levels") or {})
        indices = dict(out.get("indices") or {})

        def merge_tl(key: str, fact: dict[str, Any] | None) -> dict[str, Any] | None:
            if not fact:
                return llm_tl.get(key)
            prev = dict(llm_tl.get(key) or {})
            row = {
                **prev,
                "supports": fact.get("supports", []),
                "resistances": fact.get("resistances", []),
                "pivot": fact.get("pivot"),
                "rsi_daily": fact.get("rsi_daily", prev.get("rsi_daily")),
                "trend": (prev.get("trend") or "").strip() or fact.get("trend"),
            }
            if fact.get("pivot_signal"):
                row["pivot_signal"] = fact["pivot_signal"]
            for ema in ("ema20", "ema50", "ema200"):
                if fact.get(ema) is not None:
                    row[ema] = fact[ema]
            return row

        out["technical_levels"] = {
            "nifty50": merge_tl("nifty50", tl.get("nifty50")),
            "bank_nifty": merge_tl("bank_nifty", tl.get("bank_nifty")),
        }

        for key in ("nifty50", "bank_nifty"):
            fact = tl.get(key)
            ohlc = (fact or {}).get("session_ohlc")
            if ohlc:
                row = merge_index_fact(indices.get(key), {
                    **ohlc,
                    "change_pct": (fact or {}).get("change_pct"),
                })
                if row:
                    indices[key] = row
        out["indices"] = indices

    deriv_out = out.get("derivatives")
    if isinstance(deriv_out, dict):
        for k in ("nifty_futures_close", "premium_discount", "key_call_wall", "key_put_wall", "pcr", "max_pain"):
            if deriv_out.get(k) == 0:
                deriv_out[k] = None

    return finalize_eod_report(out, enrich, live_quote)
