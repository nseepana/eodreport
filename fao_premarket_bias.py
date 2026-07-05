#!/usr/bin/env python3
from __future__ import annotations

"""
Parse an NSE F&O end-of-day archive folder into a pre-market bias table.

Reads the daily "Reports-Archives" bundle (participant OI/volume, F&O ban list,
applicable ELM margins, daily volatility) and prints three things:

  1. Directional bias  — participant-wise net long/short in INDEX + STOCK futures
                         (Client / DII / FII / Pro). This is the "who is net
                         long vs short" institutional positioning gauge.
  2. Trade filter      — F&O ban list + high-margin (additional ELM > 0) names.
                         Check before entering any single-stock F&O trade.
  3. Volatility regime — most / least volatile underlyings by annualised vol,
                         for position sizing.

Files consumed (auto-discovered by name inside the folder):
  fao_participant_oi_*.csv    participant open interest  (standing positions)
  fao_participant_vol_*.csv   participant traded volume  (day flow) [optional]
  fo_secban_*.csv             securities in F&O ban       [optional]
  ael_*.csv                   applicable ELM margin %     [optional]
  FOVOLT_*.csv                daily / annualised vol      [optional]

Usage:
  python3 fao_premarket_bias.py                # newest bundle under ./pulled/nse-fao
  python3 fao_premarket_bias.py <folder>
  python3 fao_premarket_bias.py <folder> --top 15
  python3 fao_premarket_bias.py <folder> --json

Human-readable table on stdout; --json emits a machine-readable dict.
"""

import argparse
import csv
import glob
import json
import os
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_DEFAULT_PARENT = str(_REPO_ROOT / "pulled" / "nse-fao")


# ---------------------------------------------------------------------------
# Small parse helpers
# ---------------------------------------------------------------------------


def _to_int(v: Any) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _to_float(v: Any) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _find_one(folder: str, pattern: str) -> str | None:
    hits = sorted(glob.glob(os.path.join(folder, pattern)))
    return hits[0] if hits else None


def _read_rows(path: str, skip: int = 0) -> list[list[str]]:
    """Read CSV rows, stripping each cell. `skip` drops leading title rows."""
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.reader(fh))
    rows = rows[skip:]
    return [[c.strip() for c in r] for r in rows]


def latest_bundle(parent: str) -> str | None:
    """Return the newest Reports-Archives-Multiple-* folder under *parent*."""
    hits = sorted(glob.glob(os.path.join(parent, "Reports-Archives-Multiple-*")))
    return hits[-1] if hits else None


# ---------------------------------------------------------------------------
# 1. Participant positioning (OI / volume)
# ---------------------------------------------------------------------------

_P_COLS = [
    "client_type",
    "fut_idx_long", "fut_idx_short",
    "fut_stk_long", "fut_stk_short",
    "opt_idx_call_long", "opt_idx_put_long",
    "opt_idx_call_short", "opt_idx_put_short",
    "opt_stk_call_long", "opt_stk_put_long",
    "opt_stk_call_short", "opt_stk_put_short",
    "total_long", "total_short",
]


@dataclass
class ParticipantRow:
    client_type: str
    fut_idx_long: int = 0
    fut_idx_short: int = 0
    fut_stk_long: int = 0
    fut_stk_short: int = 0

    @property
    def idx_net(self) -> int:
        return self.fut_idx_long - self.fut_idx_short

    @property
    def stk_net(self) -> int:
        return self.fut_stk_long - self.fut_stk_short

    @property
    def idx_long_pct(self) -> float | None:
        tot = self.fut_idx_long + self.fut_idx_short
        return round(self.fut_idx_long / tot * 100, 1) if tot else None

    def bias(self) -> str:
        pct = self.idx_long_pct
        if pct is None:
            return "flat"
        if pct >= 60:
            return "bullish"
        if pct <= 40:
            return "bearish"
        return "neutral"


def parse_participants(path: str) -> list[ParticipantRow]:
    rows = _read_rows(path, skip=2)  # drop title + header
    out: list[ParticipantRow] = []
    for r in rows:
        if len(r) < 5 or not r[0] or r[0].upper() == "TOTAL":
            continue
        vals = dict(zip(_P_COLS, r))
        out.append(ParticipantRow(
            client_type=vals["client_type"],
            fut_idx_long=_to_int(vals.get("fut_idx_long")) or 0,
            fut_idx_short=_to_int(vals.get("fut_idx_short")) or 0,
            fut_stk_long=_to_int(vals.get("fut_stk_long")) or 0,
            fut_stk_short=_to_int(vals.get("fut_stk_short")) or 0,
        ))
    return out


# ---------------------------------------------------------------------------
# 2. Ban list + margins
# ---------------------------------------------------------------------------


def parse_secban(path: str) -> list[str]:
    """Return list of banned symbols; empty when the file says NIL."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if "NIL" in text.upper():
        return []
    banned: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("securities"):
            continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if parts:
            banned.append(parts[-1])
    return banned


def parse_margins(path: str, additional_only: bool = True) -> list[tuple[str, float, float]]:
    """
    Return [(symbol, additional_elm%, total_elm%)] sorted by additional ELM desc.
    When additional_only, keep just names carrying an add-on (volatile / flagged).
    """
    rows = _read_rows(path, skip=1)
    best: dict[str, tuple[float, float]] = {}
    for r in rows:
        if len(r) < 6:
            continue
        sym = r[1]
        add = _to_float(r[4])
        total = _to_float(r[5])
        if not sym or add is None or total is None:
            continue
        if sym not in best or total > best[sym][1]:
            best[sym] = (add, total)
    items = [(s, a, t) for s, (a, t) in best.items()]
    if additional_only:
        items = [x for x in items if x[1] > 0]
    items.sort(key=lambda x: (-x[1], -x[2], x[0]))
    return items


# ---------------------------------------------------------------------------
# 3. Volatility regime
# ---------------------------------------------------------------------------


def parse_volatility(path: str) -> list[tuple[str, float]]:
    """Return [(symbol, annualised_vol_pct)] sorted high→low."""
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header
        out: list[tuple[str, float]] = []
        for r in reader:
            if len(r) < 16:
                continue
            sym = r[1].strip()
            ann = _to_float(r[15])  # Applicable Annualised Volatility (N)
            if sym and ann is not None:
                out.append((sym, round(ann * 100, 2)))
    out.sort(key=lambda x: -x[1])
    return out


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------


@dataclass
class Bundle:
    folder: str
    participants_oi: list[ParticipantRow] = field(default_factory=list)
    participants_vol: list[ParticipantRow] = field(default_factory=list)
    banned: list[str] = field(default_factory=list)
    high_margin: list[tuple[str, float, float]] = field(default_factory=list)
    volatility: list[tuple[str, float]] = field(default_factory=list)


def load_bundle(folder: str) -> Bundle:
    b = Bundle(folder=folder)
    if (p := _find_one(folder, "fao_participant_oi_*.csv")):
        b.participants_oi = parse_participants(p)
    if (p := _find_one(folder, "fao_participant_vol_*.csv")):
        b.participants_vol = parse_participants(p)
    if (p := _find_one(folder, "fo_secban_*.csv")):
        b.banned = parse_secban(p)
    if (p := _find_one(folder, "ael_*.csv")):
        b.high_margin = parse_margins(p, additional_only=True)
    if (p := _find_one(folder, "FOVOLT_*.csv")):
        b.volatility = parse_volatility(p)
    return b


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_num(n: int) -> str:
    return f"{n:+,}"


def render_text(b: Bundle, top: int) -> str:
    lines: list[str] = []
    lines.append(f"NSE F&O pre-market bias  —  {os.path.basename(b.folder.rstrip('/'))}")
    lines.append("=" * 72)

    lines.append("\n1. FUTURES POSITIONING (Open Interest, net = long − short)")
    lines.append(f"   {'Participant':<8} {'IdxFut net':>14} {'Idx long%':>10} {'bias':>9} {'StkFut net':>14}")
    lines.append("   " + "-" * 60)
    for r in b.participants_oi:
        pct = f"{r.idx_long_pct}%" if r.idx_long_pct is not None else "-"
        lines.append(
            f"   {r.client_type:<8} {_fmt_num(r.idx_net):>14} {pct:>10} "
            f"{r.bias():>9} {_fmt_num(r.stk_net):>14}"
        )
    fii = next((r for r in b.participants_oi if r.client_type.upper() == "FII"), None)
    pro = next((r for r in b.participants_oi if r.client_type.upper() == "PRO"), None)
    if fii or pro:
        lines.append("")
        if fii:
            lines.append(f"   → FII index futures: {fii.bias().upper()} "
                         f"(net {_fmt_num(fii.idx_net)} contracts, {fii.idx_long_pct}% long)")
        if pro:
            lines.append(f"   → Pro index futures: {pro.bias().upper()} "
                         f"(net {_fmt_num(pro.idx_net)} contracts, {pro.idx_long_pct}% long)")

    lines.append("\n2. TRADE FILTER")
    if b.banned:
        lines.append(f"   F&O BAN ({len(b.banned)}): " + ", ".join(b.banned))
    else:
        lines.append("   F&O ban: NIL (no symbol restricted)")
    if b.high_margin:
        lines.append(f"   High-margin (additional ELM > 0) — top {min(top, len(b.high_margin))}:")
        lines.append(f"      {'Symbol':<14} {'add ELM%':>9} {'total ELM%':>11}")
        for sym, add, total in b.high_margin[:top]:
            lines.append(f"      {sym:<14} {add:>8.2f}% {total:>10.2f}%")
    else:
        lines.append("   High-margin: none flagged")

    lines.append("\n3. VOLATILITY REGIME (applicable annualised vol)")
    if b.volatility:
        lines.append(f"   Most volatile (top {min(top, len(b.volatility))}):")
        for sym, ann in b.volatility[:top]:
            lines.append(f"      {sym:<14} {ann:>6.2f}%")
        lines.append(f"   Least volatile (bottom {min(top, len(b.volatility))}):")
        for sym, ann in b.volatility[-top:][::-1]:
            lines.append(f"      {sym:<14} {ann:>6.2f}%")
    else:
        lines.append("   (no volatility file found)")

    return "\n".join(lines)


def to_dict(b: Bundle, top: int) -> dict[str, Any]:
    return {
        "folder": b.folder,
        "positioning": [
            {
                "participant": r.client_type,
                "idx_fut_net": r.idx_net,
                "idx_long_pct": r.idx_long_pct,
                "bias": r.bias(),
                "stk_fut_net": r.stk_net,
            }
            for r in b.participants_oi
        ],
        "ban_list": b.banned,
        "high_margin": [
            {"symbol": s, "additional_elm_pct": a, "total_elm_pct": t}
            for s, a, t in b.high_margin[:top]
        ],
        "most_volatile": [
            {"symbol": s, "annualised_vol_pct": v} for s, v in b.volatility[:top]
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Parse an NSE F&O archive folder into a pre-market bias table",
    )
    ap.add_argument("folder", nargs="?", default=None,
                    help=f"Archive folder (default: newest bundle under {_DEFAULT_PARENT})")
    ap.add_argument("--top", "-n", type=int, default=10,
                    help="Rows to show for margin / volatility lists (default 10)")
    ap.add_argument("--json", "-j", action="store_true", help="Emit JSON instead of a table")
    args = ap.parse_args()

    folder = args.folder or latest_bundle(_DEFAULT_PARENT)
    if not folder or not os.path.isdir(folder):
        print(f"Error: no bundle found (looked in {_DEFAULT_PARENT}). "
              f"Run fao_download_reports.py first, or pass a folder.", file=sys.stderr)
        return 1

    bundle = load_bundle(folder)

    if args.json:
        print(json.dumps(to_dict(bundle, args.top), indent=2))
    else:
        print(render_text(bundle, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
