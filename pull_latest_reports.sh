#!/usr/bin/env bash
# Pull latest EOD / pre-market / F&O-bias reports from MongoDB to local JSON files.
#
# Usage:
#   ./pull_latest_reports.sh                 # all collections, newest session
#   ./pull_latest_reports.sh --eod           # eod_reports only
#   ./pull_latest_reports.sh --premarket     # premarket_reports only
#   ./pull_latest_reports.sh --fao           # fao_daily_bias only
#   ./pull_latest_reports.sh --date 2026-07-03
#   ./pull_latest_reports.sh --out-dir ./data
#   ./pull_latest_reports.sh --sync          # also push to MONGODB_URI_IS (manual — no timer)
#
# Note the collections are dated on DIFFERENT clocks, so a bare run will often
# pull different dates per collection — that is correct, not a bug:
#   eod_reports       reportDate = the session it PLANS for (built 19:00 IST on D-1)
#   premarket_reports reportDate = the session it snapshots (09:12 IST, same day)
#   fao_daily_bias    reportDate = the session the OI data BELONGS to (21:15 IST)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"


PYTHON="${ROOT}/.venv/bin/python"
OUT_DIR="${ROOT}/pulled"
REPORT_DATE=""
PULL_EOD=0
PULL_PREMARKET=0
PULL_FAO=0
SYNC_IS=0

usage() {
  cat <<'EOF'
Pull latest generated reports from MongoDB
(eod_reports, premarket_reports, fao_daily_bias).

Options:
  --eod           Pull EOD session plan only
  --premarket     Pull pre-market snapshot only
  --fao           Pull NSE F&O participant-OI bias only
  --all           Pull all three (default when none of --eod/--premarket/--fao)
  --date YYYY-MM-DD   Specific session date (default: newest reportDate)
                      Use "latest" / "newest" to force the newest reportDate
  --out-dir DIR   Output directory (default: ./pulled)
  --sync          Upsert pulled report(s) into MONGODB_URI_IS (manual — no systemd timer)
  -h, --help      Show this help

Requires .env with MONGODB_URI (and optional EOD_REPORT_MONGODB_DB).
--sync also requires MONGODB_URI_IS.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --eod) PULL_EOD=1 ;;
    --premarket) PULL_PREMARKET=1 ;;
    --fao) PULL_FAO=1 ;;
    --all) PULL_EOD=1; PULL_PREMARKET=1; PULL_FAO=1 ;;
    --date)
      REPORT_DATE="${2:?--date requires YYYY-MM-DD}"
      shift
      ;;
    --out-dir)
      OUT_DIR="${2:?--out-dir requires a path}"
      shift
      ;;
    --sync) SYNC_IS=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

if [[ "$PULL_EOD" -eq 0 && "$PULL_PREMARKET" -eq 0 && "$PULL_FAO" -eq 0 ]]; then
  PULL_EOD=1
  PULL_PREMARKET=1
  PULL_FAO=1
fi

# "--date latest" / "--date newest" are aliases for the default (newest doc).
REPORT_DATE_LC="$(printf '%s' "$REPORT_DATE" | tr '[:upper:]' '[:lower:]')"
if [[ "$REPORT_DATE_LC" == "latest" || "$REPORT_DATE_LC" == "newest" ]]; then
  REPORT_DATE=""
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [[ ! -f "${ROOT}/.env" ]]; then
  echo "Missing .env — copy .env.example and set MONGODB_URI" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

export PULL_EOD PULL_PREMARKET PULL_FAO REPORT_DATE OUT_DIR SYNC_IS

exec "$PYTHON" - <<'PY'
from __future__ import annotations

import os
import sys
from typing import Any

from bson import json_util
from pymongo import MongoClient

from eod_report.config import EodReportConfig

cfg = EodReportConfig.from_env()
if not cfg.mongodb_uri:
    print("MONGODB_URI is not set in .env", file=sys.stderr)
    sys.exit(1)

pull_eod = os.environ.get("PULL_EOD") == "1"
pull_premarket = os.environ.get("PULL_PREMARKET") == "1"
pull_fao = os.environ.get("PULL_FAO") == "1"
sync_is = os.environ.get("SYNC_IS") == "1"
report_date = os.environ.get("REPORT_DATE", "").strip()
out_dir = os.environ["OUT_DIR"]

client = MongoClient(cfg.mongodb_uri, serverSelectionTimeoutMS=8000)
db = client[cfg.mongodb_db]

dest_client = None
dest_db = None
if sync_is:
    dest_uri = os.environ.get("MONGODB_URI_IS", "").strip()
    if not dest_uri:
        print("MONGODB_URI_IS is not set in .env", file=sys.stderr)
        sys.exit(1)
    dest_client = MongoClient(dest_uri, serverSelectionTimeoutMS=8000)
    dest_db = dest_client[cfg.mongodb_db]


def fetch_one(collection: str) -> dict[str, Any] | None:
    coll = db[collection]
    try:
        if report_date:
            # Pinned date: newest GENERATION of that session (regenerating a day
            # should supersede the earlier attempt).
            return coll.find({"reportDate": report_date}).sort("_id", -1).limit(1).next()
        # No date: newest SESSION, not newest write. Sorting by _id alone meant
        # backfilling an older report made it "latest" — regenerating 2026-07-16
        # after 2026-07-17 shadowed the current plan and --sync pushed the stale
        # day to MONGODB_URI_IS. reportDate is YYYY-MM-DD, so it sorts
        # lexicographically; _id breaks ties toward the newest generation.
        return coll.find({}).sort([("reportDate", -1), ("_id", -1)]).limit(1).next()
    except StopIteration:
        return None


def sync_one(collection: str, doc: dict[str, Any]) -> None:
    if dest_db is None:
        return
    ymd = str(doc.get("reportDate") or "").strip()
    if not ymd:
        raise ValueError(f"{collection}: document missing reportDate")
    payload = {k: v for k, v in doc.items() if k != "_id"}
    coll = dest_db[collection]
    coll.delete_many({"reportDate": ymd})
    coll.insert_one(payload)


def write_json(path: str, doc: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json_util.dumps(doc, indent=2, ensure_ascii=False))
        fh.write("\n")


written: list[str] = []
synced: list[str] = []

if pull_eod:
    doc = fetch_one("eod_reports")
    if not doc:
        print("No eod_reports document found.", file=sys.stderr)
    else:
        suffix = report_date or str(doc.get("reportDate", "latest"))
        path = os.path.join(out_dir, f"eod_{suffix}.json")
        write_json(path, doc)
        written.append(path)
        summary = (doc.get("report") or {}).get("market_summary") or {}
        print(
            f"eod_reports  date={doc.get('reportDate')} "
            f"bias={summary.get('overall_bias', '—')} → {path}"
        )
        if sync_is:
            sync_one("eod_reports", doc)
            synced.append(f"eod_reports:{doc.get('reportDate')}")

if pull_premarket:
    doc = fetch_one("premarket_reports")
    if not doc:
        print("No premarket_reports document found.", file=sys.stderr)
    else:
        suffix = report_date or str(doc.get("reportDate", "latest"))
        path = os.path.join(out_dir, f"premarket_{suffix}.json")
        write_json(path, doc)
        written.append(path)
        print(f"premarket_reports  date={doc.get('reportDate')} → {path}")
        if sync_is:
            sync_one("premarket_reports", doc)
            synced.append(f"premarket_reports:{doc.get('reportDate')}")

if pull_fao:
    doc = fetch_one("fao_daily_bias")
    if not doc:
        print("No fao_daily_bias document found.", file=sys.stderr)
    else:
        suffix = report_date or str(doc.get("reportDate", "latest"))
        path = os.path.join(out_dir, f"fao_bias_{suffix}.json")
        write_json(path, doc)
        written.append(path)
        report = doc.get("report") or {}
        summary = report.get("summary") or {}
        rows = len(report.get("positioning") or [])
        print(
            f"fao_daily_bias  date={doc.get('reportDate')} "
            f"fii={summary.get('fii_index_bias', '—')} rows={rows} → {path}"
        )
        if sync_is:
            sync_one("fao_daily_bias", doc)
            synced.append(f"fao_daily_bias:{doc.get('reportDate')}")

if not written:
    sys.exit(2)

print(f"Wrote {len(written)} file(s) to {out_dir}")
if synced:
    print(f"Synced {len(synced)} document(s) to MONGODB_URI_IS ({cfg.mongodb_db}): {', '.join(synced)}")
PY
