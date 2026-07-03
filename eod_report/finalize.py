"""Trade setups, session plan, and report finalization (port of eod-setups + eod-session-plan)."""

from __future__ import annotations

import re
from typing import Any

from eod_report.indicators import round2


def is_recap_heavy_theme(theme: str | None) -> bool:
    if not theme or not theme.strip():
        return False
    t = theme.lower()
    if re.search(
        r"expect|gap|open|overnight|hold|break|reclaim|pivot|tomorrow|pre-open|us s&p|gift",
        t,
        re.I,
    ):
        return False
    return bool(re.search(r"lead|gained|rose|closed|rally|session|\+|psu bank|metal|recovery", t, re.I))


def _fmt(n: float) -> str:
    return f"{n:,.2f}"


def _pct(n: float | None) -> str:
    if n is None:
        return "—"
    return f"{n:+.2f}%"


def is_gap_down_expected(ctx: dict[str, Any] | None) -> bool:
    if not ctx:
        return False
    if ctx.get("gap_read") == "gap_down":
        return True
    gift = ctx.get("nifty_gap_pct")
    if gift is not None and gift <= -0.3:
        return True
    sp = ctx.get("us_sp500_pct")
    if ctx.get("gap_read") != "gap_up" and gift is None and sp is not None and sp <= -0.5:
        return True
    return False


def close_vs_pivot_zone(close: float | None, fact: dict[str, Any] | None) -> str:
    if close is None or not fact or fact.get("pivot") is None:
        return "unknown"
    pivot = float(fact["pivot"])
    supports = fact.get("supports") or []
    resistances = fact.get("resistances") or []
    s1 = supports[0] if supports else None
    r1 = resistances[0] if resistances else None
    if r1 is not None and close >= r1:
        return "above_r1"
    if close > pivot:
        return "above_pivot"
    if s1 is not None and close >= s1:
        return "between_s1_pivot"
    return "below_s1"


def _setup_bias(trend: str | None, session_pct: float | None, ad_ratio: str | None) -> str:
    if not trend or trend == "indeterminate":
        return "no-trade"
    m = re.match(r"^([\d.]+)", ad_ratio or "")
    breadth = float(m.group(1)) if m else None
    strong = (session_pct or 0) >= 0.75
    weak = (session_pct or 0) <= -0.75
    broad = (breadth or 0) >= 1.25
    if trend.startswith("bullish") or trend.startswith("sideways-to-positive"):
        return "buy"
    if trend.startswith("bearish"):
        if weak or (not strong and (breadth or 0) < 1):
            return "sell"
        return "no-trade"
    if strong and broad:
        return "buy"
    if weak and not broad:
        return "sell"
    return "no-trade"


def _rr(entry: float, stop: float, target: float) -> str:
    risk = abs(stop - entry)
    reward = abs(entry - target)
    if risk <= 0 or reward <= 0:
        return "—"
    return f"1:{reward / risk:.1f}"


def build_key_levels_to_watch(
    instrument: str,
    fact: dict[str, Any] | None,
    ohlc: dict[str, Any] | None,
    derivatives: dict[str, Any] | None,
) -> list[str]:
    out: list[str] = []
    prefix = "Bank Nifty" if "BANK" in instrument.upper() else "Nifty"
    if fact and fact.get("pivot") is not None:
        out.append(f"{_fmt(fact['pivot'])} {prefix} pivot (must hold)")
    if ohlc:
        for k, label in (("close", "close"), ("high", "day high"), ("low", "day low")):
            if ohlc.get(k) is not None:
                out.append(f"{_fmt(ohlc[k])} {prefix} {label}")
    if "BANK" not in instrument.upper() and derivatives:
        for k, label in (
            ("max_pain", "max pain"),
            ("key_put_wall", "put wall"),
            ("key_call_wall", "call wall"),
        ):
            if derivatives.get(k) is not None:
                out.append(f"{_fmt(derivatives[k])} {label}")
    if fact:
        for i, s in enumerate(fact.get("supports") or []):
            out.append(f"{_fmt(s)} {prefix} S{i + 1}")
        for i, r in enumerate(fact.get("resistances") or []):
            out.append(f"{_fmt(r)} {prefix} R{i + 1}")
    return out


def build_scenario_setups(
    instrument: str,
    fact: dict[str, Any] | None,
    ohlc: dict[str, Any] | None,
    ctx: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    setups: list[dict[str, Any]] = []
    if not fact:
        return setups
    supports = fact.get("supports") or []
    resistances = fact.get("resistances") or []
    if len(supports) < 2 or len(resistances) < 2:
        return setups
    s1, s2 = supports[0], supports[1]
    r1, r2 = resistances[0], resistances[1]
    pivot = fact.get("pivot")
    live_key = "bank_live_last" if "BANK" in instrument.upper() else "nifty_live_last"
    ref = ctx.get(live_key) if ctx else None
    if ref is None:
        ref = (ohlc or {}).get("close") or fact.get("close")
    session_key = "bank_change_pct" if "BANK" in instrument.upper() else "nifty_change_pct"
    session_pct = (ctx or {}).get(session_key) or fact.get("change_pct")
    bias = _setup_bias(fact.get("trend"), session_pct, (ctx or {}).get("advance_decline"))
    zone = close_vs_pivot_zone(ref, fact)
    gap_down = is_gap_down_expected(ctx)

    if gap_down and pivot is not None:
        live_tag = f"Live {_fmt(ref)}" if ref is not None and ctx and ctx.get(live_key) else "Expected gap-down"
        setups.append({
            "instrument": instrument,
            "type": "sell",
            "scenario": "gap_down_fade",
            "activation": f"{live_tag} — short failed reclaim at pivot {pivot}.",
            "entry_label": "Short trigger (pivot retest)",
            "entry": pivot,
            "stop_loss": r1,
            "targets": [s1, s2],
            "risk_reward": _rr(pivot, r1, s1),
            "rationale": f"Gap-down session — no pivot-retest longs near {pivot}.",
        })
        return setups

    if bias == "no-trade":
        setups.append({
            "instrument": instrument,
            "type": "no-trade",
            "scenario": "range",
            "rationale": f"No directional setup — trend is {fact.get('trend')}. Range S1–R1.",
        })
        return setups

    if bias == "buy":
        if zone == "above_r1":
            setups.append({
                "instrument": instrument,
                "type": "buy",
                "scenario": "breakout",
                "entry": round2(r1),
                "stop_loss": pivot or s1,
                "targets": [r2],
                "risk_reward": "breakout above R1",
                "rationale": f"Above R1 {r1} — continuation if holds on retest.",
            })
        elif pivot is not None:
            setups.append({
                "instrument": instrument,
                "type": "buy",
                "scenario": "hold_pivot",
                "entry": pivot,
                "stop_loss": s1,
                "targets": [r1, r2],
                "risk_reward": "pivot retest to R1/R2",
                "rationale": f"Buy pivot retest {pivot}; stop S1 {s1}.",
            })
    else:
        setups.append({
            "instrument": instrument,
            "type": "sell",
            "scenario": "fade_resistance",
            "entry": r1,
            "stop_loss": r2,
            "targets": [pivot or s1, s1],
            "risk_reward": "R1 fade to pivot/S1",
            "rationale": f"Short-on-rise at R1 {r1}; trend {fact.get('trend')}.",
        })
    return setups


def build_all_scenario_setups(
    levels: dict[str, Any] | None,
    indices: dict[str, Any] | None,
    ctx: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    levels = levels or {}
    indices = indices or {}
    return [
        *build_scenario_setups("NIFTY 50", levels.get("nifty50"), indices.get("nifty50"), ctx),
        *build_scenario_setups("BANK NIFTY", levels.get("bank_nifty"), indices.get("bank_nifty"), ctx),
    ]


def build_all_key_levels_to_watch(
    levels: dict[str, Any] | None,
    indices: dict[str, Any] | None,
    derivatives: dict[str, Any] | None,
) -> list[str]:
    levels = levels or {}
    indices = indices or {}
    return [
        *build_key_levels_to_watch("NIFTY 50", levels.get("nifty50"), indices.get("nifty50"), derivatives),
        *build_key_levels_to_watch("BANK NIFTY", levels.get("bank_nifty"), indices.get("bank_nifty"), None),
    ]


def _levels_for_setups(enrich: dict[str, Any] | None, report: dict[str, Any]) -> dict[str, Any] | None:
    if enrich and enrich.get("technical_levels"):
        return enrich["technical_levels"]
    tl = report.get("technical_levels") or {}
    out: dict[str, Any] = {}
    for key in ("nifty50", "bank_nifty"):
        row = tl.get(key)
        if isinstance(row, dict) and len(row.get("supports") or []) >= 2:
            out[key] = row
    return out or None


def _build_setup_context(report: dict[str, Any], enrich: dict[str, Any] | None, live_quote: dict[str, float] | None) -> dict[str, Any]:
    indices = report.get("indices") or {}
    global_us = (report.get("global_cues") or {}).get("us_markets") or {}
    overnight = report.get("overnight_signals") or {}
    gift = overnight.get("gift_nifty") or {}
    nifty = indices.get("nifty50") or {}
    bank = indices.get("bank_nifty") or {}
    tl = (enrich or {}).get("technical_levels") or {}
    return {
        "advance_decline": (enrich or {}).get("advance_decline", {}).get("ratio") or indices.get("advance_decline"),
        "nifty_change_pct": nifty.get("change_pct") or (tl.get("nifty50") or {}).get("change_pct"),
        "bank_change_pct": bank.get("change_pct") or (tl.get("bank_nifty") or {}).get("change_pct"),
        "us_sp500_pct": global_us.get("sp500_pct"),
        "gap_read": overnight.get("gap_read"),
        "nifty_gap_pct": gift.get("change_pct") or (
            overnight.get("nifty_session_change_pct")
            if overnight.get("nifty_session_change_source") == "gift_nifty"
            else None
        ),
        "nifty_live_last": (live_quote or {}).get("nifty_last"),
        "bank_live_last": (live_quote or {}).get("bank_last"),
    }


def _prior_session_recap(report: dict[str, Any]) -> str:
    indices = report.get("indices") or {}
    sectors = report.get("sector_heatmap") or []
    parts: list[str] = []
    n = (indices.get("nifty50") or {}).get("change_pct")
    b = (indices.get("bank_nifty") or {}).get("change_pct")
    if n is not None:
        parts.append(f"Nifty {_pct(n)}")
    if b is not None:
        parts.append(f"Bank {_pct(b)}")
    leaders = sorted(
        [s for s in sectors if s.get("bias") == "bullish" and s.get("change_pct") is not None],
        key=lambda x: x.get("change_pct", 0),
        reverse=True,
    )[:2]
    if leaders:
        names = [str(s.get("sector", "")).replace("Nifty ", "") for s in leaders]
        parts.append(f"{' & '.join(names)} led")
    ad = indices.get("advance_decline")
    if ad:
        parts.append(f"breadth {ad}")
    return ", ".join(parts) + "." if parts else "Prior session data unavailable."


def _build_open_view(report: dict[str, Any]) -> str:
    overnight = report.get("overnight_signals") or {}
    tl = (report.get("technical_levels") or {}).get("nifty50") or {}
    gap = (overnight.get("gap_read") or "").replace("_", " ")
    pivot = tl.get("pivot")
    if not gap:
        return "Opening bias pending pre-open / GIFT."
    open_part = f"{gap} open"
    pivot_part = f" — pivot {_fmt(pivot)}" if pivot is not None else ""
    return open_part.capitalize() + pivot_part + "."


def _priority_levels(report: dict[str, Any]) -> list[dict[str, Any]]:
    chips: list[dict[str, Any]] = []
    tl = report.get("technical_levels") or {}
    deriv = report.get("derivatives") or {}

    def push(index: str, row: dict[str, Any] | None) -> None:
        if not row:
            return
        pivot = row.get("pivot")
        supports = row.get("supports") or []
        resistances = row.get("resistances") or []
        if pivot is not None:
            chips.append({"index": index, "kind": "Pivot", "value": pivot})
        if supports:
            chips.append({"index": index, "kind": "S1", "value": supports[0]})
        if resistances:
            chips.append({"index": index, "kind": "R1", "value": resistances[0]})

    push("Nifty", tl.get("nifty50"))
    push("Bank Nifty", tl.get("bank_nifty"))
    if deriv.get("oi_expiry"):
        for k, kind in (("max_pain", "Max pain"), ("key_put_wall", "Put wall"), ("key_call_wall", "Call wall")):
            if deriv.get(k) is not None:
                chips.append({"index": "Nifty OI", "kind": kind, "value": deriv[k]})
    return chips[:10]


def build_session_plan(report: dict[str, Any]) -> dict[str, Any]:
    overnight = report.get("overnight_signals") or {}
    global_us = (report.get("global_cues") or {}).get("us_markets") or {}
    gift = overnight.get("gift_nifty") or {}
    ms = report.get("market_summary") or {}
    outlook = report.get("outlook_tomorrow") or {}
    bias = outlook.get("bias") or ms.get("overall_bias")
    gift_pct = gift.get("change_pct") or (
        overnight.get("nifty_session_change_pct")
        if overnight.get("nifty_session_change_source") == "gift_nifty"
        else None
    )
    return {
        "target_date": report.get("next_session_date") or report.get("report_date") or "",
        "prior_session_date": report.get("session_date") or "",
        "open_view": _build_open_view(report),
        "bias": bias,
        "playbook": _build_playbook(report, bias),
        "priority_levels": _priority_levels(report),
        "overnight": {
            "gap_read": overnight.get("gap_read"),
            "gap_detail": overnight.get("gap_read_detail"),
            "us_sp500_pct": global_us.get("sp500_pct"),
            "gift_change_pct": gift_pct,
            "india_vix": overnight.get("india_vix") or (report.get("macro") or {}).get("india_vix"),
        },
        "prior_session_recap": _prior_session_recap(report),
    }


def _build_playbook(report: dict[str, Any], bias: str | None) -> list[str]:
    bullets: list[str] = []
    overnight = report.get("overnight_signals") or {}
    global_us = (report.get("global_cues") or {}).get("us_markets") or {}
    tl = (report.get("technical_levels") or {}).get("nifty50") or {}
    deriv = report.get("derivatives") or {}
    setups = report.get("trade_setups") or []
    gap = (overnight.get("gap_read") or "").replace("_", " ")
    pivot = tl.get("pivot")
    if gap and pivot is not None:
        bullets.append(
            f"Gap-down holds below pivot {_fmt(pivot)} — bias {bias or 'defensive'}."
            if "down" in gap
            else f"Gap-up: pivot {_fmt(pivot)} must hold on pullback."
            if "up" in gap
            else f"Flat open: pivot {_fmt(pivot)} is the decision level."
        )
    primary = next((s for s in setups if s.get("type") in ("buy", "sell")), None)
    if primary and primary.get("entry") is not None:
        bullets.append(
            f"{primary.get('instrument')} {str(primary.get('type', '')).upper()}: "
            f"entry {_fmt(primary['entry'])}, stop {_fmt(primary.get('stop_loss', 0))}."
        )
    gift = overnight.get("gift_nifty") or {}
    if gift.get("change_pct") is not None:
        bullets.append(f"GIFT Nifty {_pct(gift['change_pct'])} vs prior close.")
    sp = global_us.get("sp500_pct")
    if sp is not None and abs(sp) >= 0.3:
        bullets.append(f"US S&P {_pct(sp)} overnight.")
    if deriv.get("max_pain") is not None and deriv.get("oi_expiry"):
        bullets.append(f"Options max pain {_fmt(deriv['max_pain'])}.")
    vix = overnight.get("india_vix") or (report.get("macro") or {}).get("india_vix")
    if vix is not None:
        bullets.append(f"India VIX {round2(vix)}.")
    return bullets[:6]


def _synthesize_analyst_note(report: dict[str, Any]) -> str | None:
    parts: list[str] = []
    overnight = report.get("overnight_signals") or {}
    ms = report.get("market_summary") or {}
    outlook = report.get("outlook_tomorrow") or {}
    bias = outlook.get("bias") or ms.get("overall_bias")
    tl = (report.get("technical_levels") or {}).get("nifty50") or {}
    deriv = report.get("derivatives") or {}
    global_us = (report.get("global_cues") or {}).get("us_markets") or {}
    if overnight.get("gap_read"):
        detail = overnight.get("gap_read_detail")
        parts.append(f"Overnight {str(overnight['gap_read']).replace('_', ' ')}" + (f" ({detail})" if detail else "") + ".")
    if bias:
        parts.append(f"Bias into next session: {bias}.")
    if tl.get("pivot") is not None:
        parts.append(f"Nifty pivot {tl['pivot']} is the primary cue.")
    if deriv.get("max_pain") is not None:
        parts.append(f"Max pain {deriv['max_pain']}.")
    if overnight.get("india_vix") is not None:
        parts.append(f"India VIX {overnight['india_vix']}.")
    sp = global_us.get("sp500_pct")
    if sp is not None and sp <= -0.5:
        parts.append(f"Overnight S&P {_pct(sp)} — watch opening gap vs pivot.")
    return " ".join(parts) if parts else None


def finalize_eod_report(
    report: dict[str, Any],
    enrich: dict[str, Any] | None = None,
    live_quote: dict[str, float] | None = None,
) -> dict[str, Any]:
    out = dict(report)
    ctx = _build_setup_context(out, enrich, live_quote)
    levels = _levels_for_setups(enrich, out)
    indices = out.get("indices") or {}
    deriv = (enrich or {}).get("derivatives") or out.get("derivatives")

    if levels:
        index_ohlc = {
            k: indices.get(k)
            for k in ("nifty50", "bank_nifty")
            if isinstance(indices.get(k), dict)
        }
        outlook = dict(out.get("outlook_tomorrow") or {})
        outlook["key_levels_to_watch"] = build_all_key_levels_to_watch(levels, index_ohlc, deriv)
        out["outlook_tomorrow"] = outlook
        out["trade_setups"] = build_all_scenario_setups(levels, index_ohlc, ctx)
    elif not isinstance(out.get("trade_setups"), list):
        out["trade_setups"] = []

    out["session_plan"] = build_session_plan(out)
    ms = dict(out.get("market_summary") or {})
    plan = out["session_plan"]
    if not str(ms.get("session_theme", "")).strip() or is_recap_heavy_theme(str(ms.get("session_theme"))):
        ms["session_theme"] = plan.get("open_view") or ms.get("session_theme")
    if not str(ms.get("key_catalyst", "")).strip():
        outlook = out.get("outlook_tomorrow") or {}
        cats = list(outlook.get("catalysts") or []) + list(out.get("catalysts") or [])
        ms["key_catalyst"] = cats[0] if cats else _synthesize_analyst_note(out)
    out["market_summary"] = ms

    if not str(out.get("analyst_note", "")).strip():
        out["analyst_note"] = _synthesize_analyst_note(out) or out.get("analyst_note")

    if not out.get("upcoming_events"):
        events: list[str] = []
        deriv_out = out.get("derivatives") or {}
        target = out.get("report_date") or out.get("next_session_date")
        if deriv_out.get("oi_expiry"):
            events.append(f"Nifty F&O expiry — {deriv_out['oi_expiry']}")
        elif target:
            events.append(f"Nifty weekly F&O expiry — week of {target}")
        if target:
            events.append(f"Target session plan — {target}")
        out["upcoming_events"] = events

    deriv_row = out.get("derivatives")
    if isinstance(deriv_row, dict) and not deriv_row.get("oi_expiry"):
        for k in ("max_pain", "key_put_wall", "key_call_wall", "pcr", "oi_trend"):
            deriv_row[k] = None

    return out
