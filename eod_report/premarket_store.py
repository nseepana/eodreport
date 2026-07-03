"""Persist pre-market reports to MongoDB."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from eod_report.config import EodReportConfig

log = logging.getLogger(__name__)

COLLECTION = "premarket_reports"
MEM_MAX = 50
_buffer: list[dict[str, Any]] = []
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


def save_premarket_report(
    cfg: EodReportConfig,
    *,
    report_date: str,
    generated_at: str | None,
    report: dict[str, Any],
) -> str:
    rec_id = str(uuid.uuid4())
    generated = generated_at or datetime.now(timezone.utc).isoformat()
    record = {
        "id": rec_id,
        "reportDate": report_date,
        "generatedAt": generated,
        "report": report,
        "createdAt": datetime.now(timezone.utc),
    }

    db = _get_db(cfg)
    if db is not None:
        try:
            coll = db[COLLECTION]
            coll.delete_many({"reportDate": report_date})
            coll.insert_one(record)
            return rec_id
        except Exception as exc:  # noqa: BLE001
            log.warning("premarket_store.mongodb_failed error=%s", exc)

    global _buffer
    _buffer = [r for r in _buffer if r.get("reportDate") != report_date]
    _buffer.insert(0, record)
    if len(_buffer) > MEM_MAX:
        _buffer = _buffer[:MEM_MAX]
    return rec_id
