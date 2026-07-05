# kite-eod-report

Cron-friendly generators for the **kiteob Market Report**: EOD session plans and pre-market snapshots. Writes to MongoDB; kiteob reads via `/api/eod-report` and `/api/market/premarket/report`.

## What runs when

| Script | Schedule (IST) | MongoDB collection | kiteob API |
|--------|----------------|-------------------|------------|
| `generate_eod_report.py` | **16:00** Mon–Fri | `eod_reports` | `GET/POST /api/eod-report` |
| `generate_premarket_report.py` | **09:12** Mon–Fri | `premarket_reports` | `GET /api/market/premarket/report` |
| `cron_fao_reports.sh` | **21:15** Mon–Fri | `fao_daily_bias` | — |

- **EOD** — full session plan (Perplexity + NSE/Kite enrichment). Needs `PERPLEXITY_API_KEY`.
- **Pre-market** — gap/breadth snapshot only (no LLM). Best inside NSE pre-open **09:00–09:15**.
- **F&O reports** — pulls the daily NSE F&O archive bundle from the CDN, prints a positioning / trade-filter / volatility table, and upserts it to the `fao_daily_bias` collection (`--db`). See `NSE_FAO_REPORTS.md`.

Live pre-open UI still polls `/api/market/premarket` (NSE on demand). Stored snapshots freeze the auction for history / post-09:15 display.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in keys — do not commit .env
```

```bash
# EOD session plan (dry-run skips Mongo write)
.venv/bin/python generate_eod_report.py --date today --dry-run
.venv/bin/python generate_eod_report.py --date today

# Pre-market snapshot
.venv/bin/python generate_premarket_report.py --dry-run
.venv/bin/python generate_premarket_report.py

# NSE F&O reports bundle → pre-market bias table (see NSE_FAO_REPORTS.md)
./cron_fao_reports.sh
```

### Pull / sync stored reports (`pull_latest_reports.sh`)

Exports the generated report docs **out of MongoDB** into `./pulled/*.json`, and
optionally mirrors them into a second cluster (`MONGODB_URI_IS`).

```bash
./pull_latest_reports.sh                     # newest eod + premarket → ./pulled
./pull_latest_reports.sh --eod --date latest # newest EOD only ("latest"/"newest" = default)
./pull_latest_reports.sh --date 2026-07-03   # a specific session
./pull_latest_reports.sh --sync --date latest  # also push newest docs to MONGODB_URI_IS
```

`--date latest` / `--date newest` are aliases for the default (newest document
per collection); pass a `YYYY-MM-DD` to pin a specific session.

### Environment (`.env`)

| Variable | Purpose |
|----------|---------|
| `PERPLEXITY_API_KEY` | Required for EOD generation |
| `MONGODB_URI` | Persist reports (`eod_reports`, `premarket_reports`) |
| `MONGODB_URI_IS` | Optional second cluster for `pull_latest_reports.sh --sync` |
| `EOD_REPORT_MONGODB_DB` | DB name (default: `zerodha`) |
| `KITE_API_KEY` / `KITE_ACCESS_TOKEN` | Live quotes for today's session plan |
| `KITE_SESSION_MONGODB_DB` | Optional: read token synced from kiteob OAuth |

## Production: hostinger-new

| Item | Value |
|------|--------|
| SSH alias | `hostinger-new` |
| Deploy path | `/root/eodreport` |
| Python | `/root/eodreport/.venv/bin/python` |
| Config | `/root/eodreport/.env` |

### Systemd timers

Units live in `systemd/`:

- `kite-eod-session-plan.service` + `.timer` → 16:00 IST weekdays
- `kite-premarket-snapshot.service` + `.timer` → 09:12 IST weekdays
- `kite-fao-reports.service` + `.timer` → 21:15 IST weekdays (F&O archive pull)

Logs:

- `/root/eodreport/eod-report.log`
- `/root/eodreport/premarket-report.log`
- `/root/eodreport/logs/fao-reports-<date>.log` (per-run) + `fao-reports.log` (systemd)

**First install (or after editing `.service` / `.timer` files):**

```bash
scp -r systemd hostinger-new:/root/eodreport/
ssh hostinger-new 'bash /root/eodreport/systemd/install-hostinger-new.sh'
```

**App code only** (no unit changes):

```bash
ssh hostinger-new 'cd /root/eodreport && git pull'
```

**`.env` only** — edit on server; next timer run picks it up. No reinstall.

### Ops

```bash
# Next scheduled runs
ssh hostinger-new 'systemctl list-timers kite-eod-session-plan.timer kite-premarket-snapshot.timer kite-fao-reports.timer'

# Manual trigger
ssh hostinger-new 'systemctl start kite-eod-session-plan.service'
ssh hostinger-new 'systemctl start kite-premarket-snapshot.service'
ssh hostinger-new 'systemctl start kite-fao-reports.service'

# Logs
ssh hostinger-new 'tail -f /root/eodreport/eod-report.log'
ssh hostinger-new 'tail -f /root/eodreport/premarket-report.log'
```

Timers use `Persistent=true`: if the VPS was down at trigger time, systemd runs the job once after boot.

## Layout

```
generate_eod_report.py          # CLI → session plan
generate_premarket_report.py    # CLI → pre-open snapshot
pull_latest_reports.sh          # Export/sync stored report docs from Mongo → ./pulled
fao_download_reports.py         # Pull NSE F&O archive bundle from the CDN
fao_premarket_bias.py           # Parse a bundle → positioning/filter/volatility table
cron_fao_reports.sh             # Wrapper: download → parse → log (run by timer)
NSE_FAO_REPORTS.md              # F&O reports: what each is, URL map, schedule
eod_report/                     # NSE, Kite, Perplexity, merge, Mongo store
kite_session_store.py           # Optional token from kiteob Mongo session
systemd/                        # Units + install-hostinger-new.sh
```

## Related repos

- **kiteob** — UI + read APIs (`app/api/eod-report/*`, `app/api/market/premarket/*`)
- **kite-trader** — trading bots; older copy of `eod_report/` may exist there but **this repo is the cron source of truth on hostinger-new**
