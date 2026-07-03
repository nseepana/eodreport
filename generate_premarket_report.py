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
from eod_report.premarket_data import fetch_premarket_report
from eod_report.premarket_store import save_premarket_report

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("generate_premarket_report")


@click.command()
@click.option("--dry-run", is_flag=True, help="Fetch and print JSON without saving to MongoDB.")
def main(dry_run: bool) -> None:
    cfg = EodReportConfig.from_env()
    report = fetch_premarket_report()
    report_date = str(report.get("report_date") or "")
    generated_at = str(report.get("as_of") or "")

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
