"""Read F&O daily bias reports from MongoDB."""

from __future__ import annotations

import logging
from typing import Any

from eod_report.config import EodReportConfig

log = logging.getLogger(__name__)

COLLECTION = "fao_daily_bias"
_client: Any = None
_db: Any = None


def _get_db(cfg: EodReportConfig) -> Any:
    global _client, _db
    if not cfg.mongodb_uri:
        return None
    if _db is None:
        from pymongo import MongoClient

        _client = MongoClient(cfg.mongodb_uri, serverSelectionTimeoutMS=5000)
        _db = _client[cfg.mongodb_db]
    return _db


def load_fao_bias(cfg: EodReportConfig, report_date: str) -> dict[str, Any] | None:
    """Latest fao_daily_bias document for reportDate (YYYY-MM-DD), or None."""
    db = _get_db(cfg)
    if db is None:
        return None
    try:
        doc = (
            db[COLLECTION]
            .find({"reportDate": report_date})
            .sort("_id", -1)
            .limit(1)
            .next()
        )
    except StopIteration:
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("fao_bias_store.load_failed date=%s error=%s", report_date, exc)
        return None

    report = doc.get("report") or {}
    return {
        "source_date": str(doc.get("reportDate") or report_date),
        "generated_at": str(doc.get("generatedAt") or ""),
        "summary": report.get("summary") or {},
        "positioning": report.get("positioning") or [],
        "ban_list": report.get("ban_list") or [],
        "high_margin": report.get("high_margin") or [],
        "most_volatile": report.get("most_volatile") or [],
        "notional_oi": report.get("notional_oi") or [],
    }
