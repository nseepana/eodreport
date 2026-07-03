#!/usr/bin/env python3
"""Generate an EOD session-plan report and persist to MongoDB."""

from __future__ import annotations

import json
import logging
import sys

import click

from eod_report.config import EodReportConfig
from eod_report.enrichment import fetch_market_enrichment, format_enrichment_for_prompt
from eod_report.kite_auth import LOGIN_HINT, ensure_kite_for_live, resolve_kite_auth
from eod_report.market_data import (
    fetch_live_market_facts,
    format_facts_for_prompt,
    has_core_indian_market_data,
)
from eod_report.session import data_session_for_target, is_live_data_session, resolve_target_session
from eod_report.merge import apply_enrichment, apply_live_facts, normalize_report
from eod_report.parse_json import parse_eod_report_json
from eod_report.perplexity import call_perplexity
from eod_report.prompts import SYSTEM_PROMPT
from eod_report.store import save_eod_report

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("generate_eod_report")


@click.command()
@click.option("--date", default="today", help="Target session YYYY-MM-DD or 'today'.")
@click.option("--dry-run", is_flag=True, help="Fetch data and call Perplexity but do not save to MongoDB.")
def main(date: str, dry_run: bool) -> None:
    cfg = EodReportConfig.from_env()
    if not cfg.perplexity_configured():
        click.echo("PERPLEXITY_API_KEY is not configured.", err=True)
        sys.exit(1)

    target = resolve_target_session(date)
    data_session = data_session_for_target(target)
    use_live_quote = is_live_data_session(data_session)

    auth = resolve_kite_auth(cfg)
    access_token = ensure_kite_for_live(auth, live_session=use_live_quote)
    if use_live_quote and not access_token:
        click.echo(auth.message or "Kite login required for today's session plan.", err=True)
        if auth.message and LOGIN_HINT not in auth.message:
            click.echo(LOGIN_HINT, err=True)
        sys.exit(2)

    log.info("target=%s data_session=%s live_quote=%s", target, data_session, use_live_quote)

    live_facts = fetch_live_market_facts(access_token, date)
    enrichment = fetch_market_enrichment(access_token, data_session, use_live_quote)

    has_index = has_core_indian_market_data(live_facts)
    has_tl = bool(
        (enrichment.get("technical_levels") or {}).get("nifty50")
        or (enrichment.get("technical_levels") or {}).get("bank_nifty")
    )
    if live_facts.get("live") and (not has_index or not has_tl):
        click.echo(
            f"Insufficient live data (index={has_index}, technical_levels={has_tl}). "
            "Check Kite token and NSE connectivity.",
            err=True,
        )
        if not auth.valid:
            click.echo(LOGIN_HINT, err=True)
        sys.exit(2)

    enrichment_block = format_enrichment_for_prompt(enrichment)
    if live_facts.get("live"):
        user_content = f"""{format_facts_for_prompt(live_facts)}

{enrichment_block}

TARGET SESSION: {live_facts['report_date']} | PRIOR SESSION DATA: {live_facts['session_date']} | Trading day rolls 09:15 IST.

Generate the report JSON.

NUMBERS: Copy VERIFIED LIVE DATA + COMPUTED INDICATORS exactly (indices, sectors, macro, FII-DII, global, top_movers names/%, advance_decline, technical_levels, derivatives). Set trade_setups to [].

OVERNIGHT PLAN (tomorrow is the product — yesterday is context):
1. Copy TOMORROW OPEN SIGNALS into overnight_signals exactly. overall_bias / outlook_tomorrow.bias from gap + US/VIX — NOT from yesterday's +0.4% move.
2. market_summary.session_theme = expected OPEN (gap up/down/flat + pivot), max one short clause on yesterday.
3. outlook_tomorrow.risks/catalysts = what matters for {live_facts['report_date']} open and session — not a recap of {live_facts['session_date']}.
4. analyst_note: gap, US S&P, VIX, pivot hold/break for tomorrow's open.
5. TOP MOVERS: web-search each for {live_facts['session_date']} (yesterday's move — one line each).
6. Do not invent numbers. Use null when absent.

Set session_date={live_facts['session_date']}, report_date={live_facts['report_date']}, next_session_date={live_facts['report_date']}. Return ONLY the JSON."""
    else:
        user_content = (
            f"Generate a report FOR the NSE/BSE trading session on {live_facts['report_date']} (target), "
            f"analyzed from the prior completed session {live_facts['session_date']}. "
            f"Set session_date={live_facts['session_date']}, report_date={live_facts['report_date']}, "
            f"next_session_date={live_facts['report_date']}. Return ONLY the JSON."
        )

    pp = call_perplexity(
        cfg,
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        session_date=live_facts.get("session_date"),
        next_session_date=live_facts.get("report_date"),
    )
    if pp.get("error"):
        click.echo(f"Perplexity error: {pp['error']}", err=True)
        sys.exit(3)
    if pp.get("finish_reason") == "length":
        click.echo("Model response truncated (token limit).", err=True)
        sys.exit(3)

    parsed = parse_eod_report_json(pp.get("text", ""))
    if not parsed.get("ok"):
        reason = parsed.get("reason", "invalid")
        detail = parsed.get("detail", "")
        click.echo(f"JSON parse failed ({reason}): {detail}", err=True)
        sys.exit(4)

    live_quote = None
    preopen = live_facts.get("preopen_index") or {}
    if preopen.get("nifty50") or preopen.get("bank_nifty"):
        live_quote = {
            "nifty_last": preopen.get("nifty50"),
            "bank_last": preopen.get("bank_nifty"),
        }

    report = normalize_report(
        apply_enrichment(
            apply_live_facts(parsed["value"], live_facts),
            enrichment,
            live_quote,
        )
    )

    report_date = str(report.get("report_date") or live_facts["report_date"])
    generated_at = str(report.get("generated_at") or live_facts["fetched_at"])
    live_data = {
        **live_facts,
        "sources": list(dict.fromkeys([
            *(live_facts.get("sources") or []),
            *(enrichment.get("computed_from") or []),
        ])),
    }

    cost = pp.get("cost")
    sources = pp.get("sources") or []

    if dry_run:
        click.echo(json.dumps({
            "dry_run": True,
            "report_date": report_date,
            "report": report,
            "cost": cost,
            "sources": sources,
            "live_data": live_data,
        }, indent=2, default=str))
        return

    if not cfg.mongodb_uri:
        click.echo("MONGODB_URI not set — report generated but not persisted.", err=True)

    rec_id = save_eod_report(
        cfg,
        report_date=report_date,
        generated_at=generated_at,
        report=report,
        cost=cost,
        sources=sources,
        live_data=live_data,
    )
    click.echo(json.dumps({
        "id": rec_id,
        "report_date": report_date,
        "cost": cost,
        "sources_count": len(sources),
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
