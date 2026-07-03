#!/usr/bin/env bash
# Install EOD / pre-market report timers on hostinger-new (/root/eodreport).
set -euo pipefail

ROOT="${EOD_REPORT_ROOT:-/root/eodreport}"
UNIT_DIR="$(cd "$(dirname "$0")" && pwd)"

for unit in kite-eod-session-plan kite-premarket-snapshot; do
  install -m 644 "${UNIT_DIR}/${unit}.service" "/etc/systemd/system/${unit}.service"
  install -m 644 "${UNIT_DIR}/${unit}.timer" "/etc/systemd/system/${unit}.timer"
done

systemctl daemon-reload
systemctl enable --now kite-eod-session-plan.timer
systemctl enable --now kite-premarket-snapshot.timer

echo "Installed. Next triggers:"
systemctl list-timers kite-eod-session-plan.timer kite-premarket-snapshot.timer --no-pager
