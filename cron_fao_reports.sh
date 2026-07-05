#!/usr/bin/env bash

# Daily NSE F&O reports pull + pre-market bias parse.
# Downloads the report bundle for a trade date (default: today IST), then prints
# the positioning / filter / volatility table. Scheduled via the
# kite-fao-reports systemd timer. Output is appended to
# logs/fao-reports-<YYYY-MM-DD>.log at the repo root.
#
# Env overrides:
#   FAO_DATE   trade date (DD-MM-YYYY / YYYY-MM-DD); default = today IST
#   FAO_ONLY   comma-separated report keys (only used when FAO_ALL != 1)
#   FAO_OUT    parent output dir; default = downloader built-in (repo/pulled/nse-fao)
#   FAO_ALL=1  pull the full bundle (default here); unset to use FAO_ONLY lean set
#   FAO_DB=1   upsert the parsed bias report into MongoDB (fao_daily_bias); default on

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOG_DIR="${ROOT}/logs"
mkdir -p "${LOG_DIR}"
STAMP="$(TZ=Asia/Kolkata date +%Y-%m-%d)"
LOG_FILE="${LOG_DIR}/fao-reports-${STAMP}.log"

if [[ -z "${PYTHON_BIN:-}" ]]; then
    if [[ -x "${ROOT}/.venv/bin/python" ]]; then
        PYTHON_BIN="${ROOT}/.venv/bin/python"
    else
        PYTHON_BIN="/usr/bin/env python3"
    fi
fi

FAO_ONLY="${FAO_ONLY:-participant_oi,secban,fovolt,settlement}"

exec >>"${LOG_FILE}" 2>&1

echo "===== $(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S %z') start ====="
cd "${ROOT}"

DL_ARGS=()
[[ -n "${FAO_DATE:-}" ]] && DL_ARGS+=(--date "${FAO_DATE}")
[[ -n "${FAO_OUT:-}" ]]  && DL_ARGS+=(--out "${FAO_OUT}")
if [[ "${FAO_ALL:-1}" != "1" ]]; then
    DL_ARGS+=(--only "${FAO_ONLY}")
fi

echo "downloader args: ${DL_ARGS[*]:-<none>}"

FOLDER="$(${PYTHON_BIN} fao_download_reports.py "${DL_ARGS[@]}" | tail -n 1)"
echo "bundle folder: ${FOLDER}"

if [[ -d "${FOLDER}" ]]; then
    echo "----- pre-market bias -----"
    BIAS_ARGS=("${FOLDER}")
    [[ "${FAO_DB:-1}" == "1" ]] && BIAS_ARGS+=(--db)
    ${PYTHON_BIN} fao_premarket_bias.py "${BIAS_ARGS[@]}" || true
else
    echo "WARN: no bundle folder produced (nothing published yet?)"
fi

echo "===== $(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S %z') done ====="
