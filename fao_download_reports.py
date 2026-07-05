#!/usr/bin/env python3
from __future__ import annotations

"""
Download the daily NSE F&O "Reports-Archives" bundle for a given trade date.

Mirrors the files behind
https://www.nseindia.com/all-reports-derivatives#cr_deriv_equity_archives
by hitting the NSE archive CDN directly (browser-like TLS + warmed cookies).

Confirmed report URLs (paths verified live against the CDN):

  key             file                              CDN path
  --------------  --------------------------------  ----------------------------------------
  participant_oi  fao_participant_oi_<d>.csv        content/nsccl/
  participant_vol fao_participant_vol_<d>.csv       content/nsccl/
  top10cm         fao_top10cm_to_<d>.csv            content/nsccl/
  contract_delta  Contract_Delta_<d>.csv            content/nsccl/
  combineoi       combineoi_deleq_<d>.csv           archives/nsccl/mwpl/
  oi_cli_limit    oi_cli_limit_<DD-MON-YYYY>.lst    content/nsccl/
  fovolt          FOVOLT_<d>.csv                    archives/nsccl/volt/
  settlement      FOSett_prce_<d>.csv               archives/nsccl/sett/
  secban          fo_secban_<d>.csv                 archives/fo/sec_ban/   (named by the trade date it applies to)
  bhavcopy        BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip   content/fo/  (UDiFF, replaces legacy FNO_BC .DAT)

  <d> = DDMMYYYY.  `ael` (applicable ELM margins) is NOT on the public CDN under
  standard paths — it's served from the NSE member portal — so it is skipped.

Usage:
  python3 fao_download_reports.py                 # today (IST)
  python3 fao_download_reports.py --date 03-07-2026
  python3 fao_download_reports.py --date 2026-07-03 --out /path/to/dir
  python3 fao_download_reports.py --only participant_oi,fovolt,secban

Files land in <out>/Reports-Archives-Multiple-<DDMMYYYY>/ (default out = ./pulled/nse-fao).
A 404 means the report isn't published yet for that date (skipped, non-fatal).
"""

import argparse
import datetime
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Callable
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_CDN = "https://nsearchives.nseindia.com"
_WARM_URL = "https://www.nseindia.com/all-reports-derivatives"
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_DEFAULT_OUT = str(_REPO_ROOT / "pulled" / "nse-fao")


# ---------------------------------------------------------------------------
# Date parsing / formatting
# ---------------------------------------------------------------------------


def parse_date(s: str | None) -> datetime.date:
    """Accept DD-MM-YYYY, YYYY-MM-DD, DDMMYYYY, or None (=today IST)."""
    if not s:
        return datetime.datetime.now(IST).date()
    s = s.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d%m%Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {s!r} (use DD-MM-YYYY or YYYY-MM-DD)")


def d_ddmmyyyy(d: datetime.date) -> str:
    return d.strftime("%d%m%Y")


def d_yyyymmdd(d: datetime.date) -> str:
    return d.strftime("%Y%m%d")


def d_dd_mon_yyyy(d: datetime.date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


# ---------------------------------------------------------------------------
# Report registry: key -> (url, output filename)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    key: str
    build: Callable[[datetime.date], tuple[str, str]]  # date -> (url, out_name)


def _reports() -> list[Report]:
    def r(key: str, path: str, name_fn: Callable[[datetime.date], str]) -> Report:
        def build(d: datetime.date) -> tuple[str, str]:
            name = name_fn(d)
            return f"{_CDN}/{path}/{name}", name
        return Report(key, build)

    dd = d_ddmmyyyy
    return [
        r("participant_oi",  "content/nsccl",       lambda d: f"fao_participant_oi_{dd(d)}.csv"),
        r("participant_vol", "content/nsccl",       lambda d: f"fao_participant_vol_{dd(d)}.csv"),
        r("top10cm",         "content/nsccl",       lambda d: f"fao_top10cm_to_{dd(d)}.csv"),
        r("contract_delta",  "content/nsccl",       lambda d: f"Contract_Delta_{dd(d)}.csv"),
        r("combineoi",       "archives/nsccl/mwpl", lambda d: f"combineoi_deleq_{dd(d)}.csv"),
        r("oi_cli_limit",    "content/nsccl",       lambda d: f"oi_cli_limit_{d_dd_mon_yyyy(d)}.lst"),
        r("fovolt",          "archives/nsccl/volt", lambda d: f"FOVOLT_{dd(d)}.csv"),
        r("settlement",      "archives/nsccl/sett", lambda d: f"FOSett_prce_{dd(d)}.csv"),
        r("secban",          "archives/fo/sec_ban", lambda d: f"fo_secban_{dd(d)}.csv"),
        r("bhavcopy",        "content/fo",
          lambda d: f"BhavCopy_NSE_FO_0_0_0_{d_yyyymmdd(d)}_F_0000.csv.zip"),
    ]


# ---------------------------------------------------------------------------
# HTTP session (curl_cffi Chrome fingerprint + cookie warm-up)
# ---------------------------------------------------------------------------


def _make_session():
    try:
        from curl_cffi import requests as cffi  # type: ignore
    except ImportError:
        print("Error: curl_cffi required — run: .venv/bin/pip install curl_cffi",
              file=sys.stderr)
        raise SystemExit(1)
    s = cffi.Session(impersonate="chrome124")
    try:
        s.get(_WARM_URL, timeout=20)  # seed Akamai cookies
    except Exception as exc:  # noqa: BLE001
        print(f"warn: cookie warm-up failed: {exc}", file=sys.stderr)
    return s


_RETRY = {429, 500, 502, 503, 504}


def _download(session, url: str, dest: str, timeout: float = 30.0,
              attempts: int = 4) -> tuple[bool, int, int]:
    """Return (ok, status, bytes). 404 is terminal & quiet (not published)."""
    delay = 2.0
    for i in range(1, attempts + 1):
        try:
            resp = session.get(url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — transport failure
            print(f"    transport error: {exc}", file=sys.stderr)
            resp = None
        if resp is not None and resp.status_code == 200:
            with open(dest, "wb") as fh:
                fh.write(resp.content)
            return True, 200, len(resp.content)
        status = resp.status_code if resp is not None else None
        if (status is None or status in _RETRY) and i < attempts:
            time.sleep(delay)
            delay *= 2
            continue
        return False, status or 0, 0
    return False, 0, 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download the daily NSE F&O reports archive bundle",
    )
    ap.add_argument("--date", "-d", default=None,
                    help="Trade date (DD-MM-YYYY / YYYY-MM-DD). Default: today IST")
    ap.add_argument("--out", "-o", default=_DEFAULT_OUT,
                    help=f"Parent output dir (default: {_DEFAULT_OUT})")
    ap.add_argument("--only", default=None,
                    help="Comma-separated report keys to fetch (default: all)")
    ap.add_argument("--list", action="store_true", help="List report keys and exit")
    args = ap.parse_args()

    reports = _reports()
    if args.list:
        for rep in reports:
            print(rep.key)
        return 0

    day = parse_date(args.date)
    if args.only:
        wanted = {k.strip() for k in args.only.split(",") if k.strip()}
        reports = [r for r in reports if r.key in wanted]
        missing = wanted - {r.key for r in reports}
        if missing:
            print(f"Unknown report keys: {', '.join(sorted(missing))}", file=sys.stderr)

    folder = os.path.join(args.out, f"Reports-Archives-Multiple-{d_ddmmyyyy(day)}")
    os.makedirs(folder, exist_ok=True)
    print(f"Trade date : {day.isoformat()}  ({d_ddmmyyyy(day)})", file=sys.stderr)
    print(f"Output dir : {folder}", file=sys.stderr)

    session = _make_session()
    ok = fail = 0
    for rep in reports:
        url, name = rep.build(day)
        dest = os.path.join(folder, name)
        got, status, nbytes = _download(session, url, dest)
        if got:
            ok += 1
            print(f"  ok    {rep.key:16} {name}  ({nbytes:,} bytes)", file=sys.stderr)
        else:
            fail += 1
            note = "not published yet" if status == 404 else f"HTTP {status}"
            print(f"  MISS  {rep.key:16} {name}  ({note})", file=sys.stderr)
        time.sleep(0.3)

    print(f"\nDone: {ok} downloaded, {fail} missing → {folder}", file=sys.stderr)
    print(folder)  # stdout = the folder path (pipe into the analyzer)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
