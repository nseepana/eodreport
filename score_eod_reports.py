#!/usr/bin/env python3
"""Score stored EOD reports: predicted bias/gap/levels vs realized next session.

Chain method — no market-data fetch needed: the report FOR session D is scored
against the actuals carried by the report whose session_date == D (each report
embeds the prior session's Kite-verified NIFTY OHLC). Read-only on MongoDB.

This is the evidence behind the deterministic default in generate_eod_report.py:
over 25 chained sessions (2026-06-15..07-31) the LLM overall_bias was neutral or
absent 16/25 times and 5 hit / 3 miss on directional calls; gap_read scored
11/22 = coin flip (snapshotted 16:00 IST, before the overnight session exists).
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import click

from eod_report.config import EodReportConfig
from eod_report.store import _get_db

GAP_BAND_PCT = 0.15  # |open gap| below this counts as "flat"
FLAT_BAND_PCT = 0.1  # |close-to-close| below this scores neither hit nor miss


def _fnum(x: Any) -> float | None:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _nifty(doc: dict) -> dict:
    return ((doc.get("report") or {}).get("indices") or {}).get("nifty50") or {}


def _bias_dir(bias: str) -> int:
    if "bullish" in bias:
        return 1
    if "bearish" in bias:
        return -1
    return 0


def build_rows(docs: list[dict]) -> list[dict]:
    by_session = {
        (d.get("report") or {}).get("session_date"): d
        for d in docs
        if (d.get("report") or {}).get("session_date")
    }
    rows = []
    for d in sorted(docs, key=lambda x: (x.get("report") or {}).get("next_session_date") or ""):
        r = d.get("report") or {}
        nxt = r.get("next_session_date")
        actual_doc = by_session.get(nxt)
        if not actual_doc or actual_doc is d:
            continue
        a = _nifty(actual_doc)
        ao, ah, al, ac = (_fnum(a.get(k)) for k in ("open", "high", "low", "close"))
        pc = _fnum(_nifty(d).get("close"))
        if None in (ao, ah, al, ac) or not pc:
            continue

        tl = ((r.get("technical_levels") or {}).get("nifty50") or {})
        sups = [s for s in map(_fnum, tl.get("supports") or []) if s]
        ress = [x for x in map(_fnum, tl.get("resistances") or []) if x]
        rows.append({
            "for": nxt,
            "mode": r.get("generation_mode") or "llm",
            "bias": ((r.get("market_summary") or {}).get("overall_bias") or "").lower(),
            "gap_read": ((r.get("overnight_signals") or {}).get("gap_read") or "").lower(),
            "chg_pct": (ac - pc) / pc * 100,
            "gap_pct": (ao - pc) / pc * 100,
            "low": al,
            "high": ah,
            "prior_close": pc,
            "s1": max([s for s in sups if s < pc], default=None),
            "r1": min([x for x in ress if x > pc], default=None),
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    bias_hit = bias_miss = bias_silent = 0
    gap_hit = gap_miss = gap_na = 0
    s_hold = s_break = 0
    s_dists: list[float] = []
    r_dists: list[float] = []
    for row in rows:
        bd = _bias_dir(row["bias"])
        if bd == 0 or abs(row["chg_pct"]) < FLAT_BAND_PCT:
            bias_silent += 1
        elif bd * row["chg_pct"] > 0:
            bias_hit += 1
        else:
            bias_miss += 1

        g = row["gap_read"]
        if not g:
            gap_na += 1
        else:
            pred = 1 if "up" in g else (-1 if "down" in g else 0)
            act = 1 if row["gap_pct"] >= GAP_BAND_PCT else (-1 if row["gap_pct"] <= -GAP_BAND_PCT else 0)
            if pred == act:
                gap_hit += 1
            else:
                gap_miss += 1

        if row["s1"]:
            s_dists.append((row["low"] - row["s1"]) / row["prior_close"] * 100)
            if row["low"] < row["s1"]:
                s_break += 1
            else:
                s_hold += 1
        if row["r1"]:
            r_dists.append((row["r1"] - row["high"]) / row["prior_close"] * 100)

    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None  # noqa: E731
    return {
        "sessions": len(rows),
        "bias": {"hit": bias_hit, "miss": bias_miss, "silent_or_flat": bias_silent,
                 "distribution": dict(Counter(r["bias"] or "(none)" for r in rows))},
        "gap_read": {"hit": gap_hit, "miss": gap_miss, "na": gap_na},
        "nearest_support": {"held": s_hold, "broken": s_break,
                            "median_low_vs_s1_pct": med(s_dists)},
        "nearest_resistance": {"median_r1_vs_high_pct": med(r_dists)},
    }


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Emit summary + rows as JSON.")
def main(as_json: bool) -> None:
    cfg = EodReportConfig.from_env()
    db = _get_db(cfg)
    if db is None:
        raise click.ClickException("MONGODB_URI not configured or unreachable.")
    docs = list(db["eod_reports"].find({}, {"_id": 0, "report": 1}))
    rows = build_rows(docs)
    summary = summarize(rows)
    if as_json:
        click.echo(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
        return
    click.echo(f"scoreable chained sessions: {summary['sessions']} (of {len(docs)} stored reports)")
    b = summary["bias"]
    click.echo(f"bias      : {b['hit']} hit / {b['miss']} miss / {b['silent_or_flat']} neutral-or-flat  {b['distribution']}")
    g = summary["gap_read"]
    click.echo(f"gap_read  : {g['hit']} hit / {g['miss']} miss / {g['na']} n/a  (±{GAP_BAND_PCT}% band)")
    s = summary["nearest_support"]
    ms = s["median_low_vs_s1_pct"]
    click.echo(f"support S1: held {s['held']} / broken {s['broken']}, median low-vs-S1 {ms:+.2f}%" if ms is not None else "support S1: no data")
    mr = summary["nearest_resistance"]["median_r1_vs_high_pct"]
    if mr is not None:
        click.echo(f"resist  R1: median R1-vs-high {mr:+.2f}%")
    click.echo(f"{'for':<12}{'mode':<14}{'bias':<22}{'chg%':>7}  {'gap_read':<10}{'gap%':>7}")
    for row in rows:
        click.echo(
            f"{row['for']:<12}{row['mode']:<14}{row['bias'] or '-':<22}"
            f"{row['chg_pct']:>7.2f}  {row['gap_read'] or '-':<10}{row['gap_pct']:>7.2f}"
        )


if __name__ == "__main__":
    main()
