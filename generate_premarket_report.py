#!/usr/bin/env python3
"""Generate a pre-open data snapshot (gap/breadth only) — NOT the session plan.

For the full Session Plan (playbook, levels, outlook), use generate_eod_report.py
which writes to MongoDB eod_reports and is what kiteob /api/eod-report loads.
"""

from __future__ import annotations

import json
import logging
import sys

import click

from eod_report.config import EodReportConfig
from eod_report.fao_bias_store import load_fao_bias
from eod_report.premarket_data import fetch_premarket_report
from eod_report.premarket_store import save_premarket_report
from eod_report.session import ist_now, prev_trading_session_iso
from eod_report.trading_calendar import is_trading_day

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("generate_premarket_report")


@click.command()
@click.option("--dry-run", is_flag=True, help="Fetch and print JSON without saving to MongoDB.")
def main(dry_run: bool) -> None:
    cfg = EodReportConfig.from_env()
    now = ist_now()
    if not is_trading_day("NSE", now):
        log.info("NSE closed today (%s) — skipping premarket snapshot", now.strftime("%Y-%m-%d"))
        return

    report = fetch_premarket_report(now)
    report_date = str(report.get("report_date") or "")
    generated_at = str(report.get("as_of") or "")

    fao_date = prev_trading_session_iso(report_date)
    fao = load_fao_bias(cfg, fao_date)
    if fao:
        report["fao_bias"] = fao
        log.info("attached fao_bias from %s", fao_date)
    else:
        log.warning("no fao_daily_bias for %s (prior session)", fao_date)

    window = report.get("window") or {}
    if not window.get("in_pre_open_window"):
        log.warning("outside 09:00–09:15 IST — snapshot may reflect prior session pre-open")

    if dry_run:
        click.echo(json.dumps(report, indent=2, default=str))
        return

    if not cfg.mongodb_uri:
        click.echo("MONGODB_URI not set — report generated but not persisted.", err=True)

    rec_id = save_premarket_report(
        cfg,
        report_date=report_date,
        generated_at=generated_at,
        report=report,
    )
    click.echo(json.dumps({
        "id": rec_id,
        "report_date": report_date,
        "bias": (report.get("market_direction") or {}).get("bias"),
        "conviction": (report.get("market_direction") or {}).get("conviction"),
        "stock_count": report.get("stock_count"),
    }, indent=2))


if __name__ == "__main__":
    main()
