# NSE F&O Daily Reports — Pull, Parse, Schedule

Source page: <https://www.nseindia.com/all-reports-derivatives#cr_deriv_equity_archives>

Two scripts + a systemd timer turn NSE's daily F&O archive into a pre-market bias table.
Deployed inside this repo on the server at `/root/eodreport`.

| File | Job |
|---|---|
| `fao_download_reports.py` | Pull the daily report bundle from NSE's archive CDN |
| `fao_premarket_bias.py` | Parse a bundle → positioning + trade filter + volatility |
| `cron_fao_reports.sh` | Wrapper: download → parse → log (run by the timer) |
| `systemd/kite-fao-reports.{service,timer}` | Daily 21:15 IST auto-run |

---

## 1. What each report is (and how it helps)

| # | File | Content | Trading use |
|---|---|---|---|
| 1 | `fao_participant_oi_*.csv` | Open interest by participant (Client/DII/FII/Pro), fut + opt long/short | **Directional bias** — who is net long vs short |
| 2 | `fao_participant_vol_*.csv` | Same split, traded **volume** (day flow) | Fresh positioning vs standing OI |
| 3 | `fo_secban_*.csv` | F&O ban list (or `NIL`) | Hard gate — no adds on banned names |
| 4 | `FOVOLT_*.csv` | Daily + annualised volatility per underlying | Position sizing / option pricing |
| 5 | `FOSett_prce_*.csv` | Daily MTM settlement prices | P&L reconciliation, backtest fills |
| 6 | `combineoi_deleq_*.csv` | Notional + futures-equivalent OI per stock | Single-stock crowding / near-ban watch |
| 7 | `Contract_Delta_*.csv` | Delta factor per contract (6.5 MB) | Delta-weight option OI (rarely needed) |
| 8 | `fao_top10cm_to_*.csv` | Top-10 clearing-member turnover | Broad liquidity trivia |
| 9 | `oi_cli_limit_*.lst` | Client-wise OI position limits | Only if running large size |
| 10 | `BhavCopy_NSE_FO_..._F_0000.csv.zip` | **UDiFF** bhavcopy — full O/H/L/C/OI (1 MB) | Master raw data for backtests |
| — | `ael_*.csv` | Applicable ELM margin % per symbol | Member-portal only — NOT on public CDN |

> Legacy `FNO_BC*.DAT` bhavcopy is retired — NSE now serves the **UDiFF** zip.

---

## 2. Lean set vs full bundle

For swing bias + F&O risk, **~4 files carry almost all the signal**:

| Tier | Files | Size |
|---|---|---|
| **Keep (lean)** | `fao_participant_oi`, `fo_secban`, `FOVOLT`, `FOSett_prce` | ~80 KB |
| Optional | `fao_participant_vol`, `combineoi_deleq` | ~55 KB |
| Skip | `Contract_Delta` (6.5 MB), `bhavcopy` (1 MB), `top10cm`, `oi_cli_limit` | ~7.6 MB |

The scheduled run uses the **full bundle** (`FAO_ALL=1`); the lean set is `--only participant_oi,secban,fovolt,settlement`.

---

## 3. Confirmed CDN URL map (live-tested)

Base: `https://nsearchives.nseindia.com`

| Report key | Path | Filename | Date fmt |
|---|---|---|---|
| `participant_oi` / `participant_vol` | `content/nsccl/` | `fao_participant_{oi,vol}_<d>.csv` | DDMMYYYY |
| `top10cm`, `contract_delta` | `content/nsccl/` | `fao_top10cm_to_<d>.csv`, `Contract_Delta_<d>.csv` | DDMMYYYY |
| `combineoi` | `archives/nsccl/mwpl/` | `combineoi_deleq_<d>.csv` | DDMMYYYY |
| `oi_cli_limit` | `content/nsccl/` | `oi_cli_limit_<DD-MON-YYYY>.lst` | DD-MON-YYYY |
| `fovolt` | `archives/nsccl/volt/` | `FOVOLT_<d>.csv` | DDMMYYYY |
| `settlement` | `archives/nsccl/sett/` | `FOSett_prce_<d>.csv` | DDMMYYYY |
| `secban` | `archives/fo/sec_ban/` | `fo_secban_<d>.csv` | DDMMYYYY (date it *applies* to) |
| `bhavcopy` | `content/fo/` | `BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip` | YYYYMMDD |

NSE needs browser-like TLS + warmed cookies → uses `curl_cffi` (Chrome fingerprint), hits the reports page first. `404` = not published yet (skipped); retries on 429/5xx.

---

## 4. Usage (local / manual)

```bash
# Download (default out = ./pulled/nse-fao)
.venv/bin/python fao_download_reports.py                        # today (IST)
.venv/bin/python fao_download_reports.py --date 03-07-2026
.venv/bin/python fao_download_reports.py --only participant_oi,secban,fovolt,settlement
.venv/bin/python fao_download_reports.py --list

# Parse (default = newest bundle under ./pulled/nse-fao)
.venv/bin/python fao_premarket_bias.py
.venv/bin/python fao_premarket_bias.py <folder> --top 15
.venv/bin/python fao_premarket_bias.py <folder> --json
.venv/bin/python fao_premarket_bias.py <folder> --db     # upsert into Mongo fao_daily_bias

# Full chain (what the timer runs — downloads full bundle, parses, writes to Mongo)
./cron_fao_reports.sh
```

### MongoDB (`--db`)

`--db` upserts the parsed report (delete-then-insert by `reportDate`, mirroring
`premarket_store.py`) into `<EOD_REPORT_MONGODB_DB>.fao_daily_bias` using the
`MONGODB_URI` from `.env`. The scheduled `cron_fao_reports.sh` sets `FAO_DB=1`
by default. Document shape:

```jsonc
{
  "id": "<uuid>",
  "reportDate": "2026-07-03",          // derived from the bundle folder DDMMYYYY
  "generatedAt": "<iso utc>",
  "report": {
    "positioning": [ { "participant": "FII", "idx_fut_net": -250767,
                       "idx_long_pct": 9.6, "bias": "bearish", "stk_fut_net": 547349 }, ... ],
    "ban_list": [],
    "high_margin": [ { "symbol": "ADANIENT", "additional_elm_pct": 15, "total_elm_pct": 20.25 } ],
    "most_volatile": [ { "symbol": "KAYNES", "annualised_vol_pct": 67.54 }, ... ],
    "summary": { "fii_index_bias": "bearish", "fii_index_long_pct": 9.6,
                 "fii_index_net": -250767, "pro_index_bias": "neutral",
                 "ban_count": 0, "most_volatile_top": "KAYNES" }
  },
  "createdAt": "<utc datetime>"
}
```

Export/sync like the other collections: `./pull_latest_reports.sh` can be
pointed at it, or query `fao_daily_bias` directly.

---

## 5. Schedule (systemd on hostinger-new, /root/eodreport)

| Timer | IST | Command |
|---|---|---|
| `kite-fao-reports.timer` | Mon–Fri 21:15 | `cron_fao_reports.sh` (full bundle) |

Deploy / re-install units (only when `systemd/*` change):

```bash
scp fao_download_reports.py fao_premarket_bias.py cron_fao_reports.sh hostinger-new:/root/eodreport/
scp -r systemd hostinger-new:/root/eodreport/
ssh hostinger-new 'bash /root/eodreport/systemd/install-hostinger-new.sh'
```

Code-only deploy: `ssh hostinger-new 'cd /root/eodreport && git pull'`.

Logs: `logs/fao-reports-<date>.log` (per-run detail) + `fao-reports.log` (systemd stdout).
Verify: `ssh hostinger-new 'systemctl start kite-fao-reports.service && tail -n 40 /root/eodreport/logs/fao-reports-$(TZ=Asia/Kolkata date +%F).log'`

---

## 6. Sample output (trade date 03-Jul-2026)

```
1. FUTURES POSITIONING (Open Interest, net = long − short)
   FII            -250,767       9.6%   bearish       +547,349
   → FII index futures: BEARISH (net -250,767 contracts, 9.6% long)

2. TRADE FILTER   →  F&O ban: NIL

3. VOLATILITY REGIME
   Most volatile:  KAYNES 67.5% · GODFRYPHLP 65.4% · PGEL 64.2%
   Least volatile: NIFTY 17.1% · SENSEX 17.2% · BANKNIFTY 21.0%
```

**Read:** FII heavily net-short index futures (9.6% long) = bearish tilt; no ban; index vol calm (~17%) vs single-stock names (60%+).
