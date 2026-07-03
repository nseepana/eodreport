"""Kite access token store — reads the same MongoDB doc as kiteob OAuth."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

COLLECTION = "kite_webhook_access"
DOC_KEY = "default"

_client: Any = None
_db: Any = None


def _session_db_name() -> str:
    return (
        os.environ.get("KITE_SESSION_MONGODB_DB", "").strip()
        or os.environ.get("EOD_REPORT_MONGODB_DB", "").strip()
        or "zerodha"
    )


def _mongo_uri() -> str:
    return os.environ.get("MONGODB_URI", "").strip()


def _get_db() -> Any:
    global _client, _db
    uri = _mongo_uri()
    if not uri:
        return None
    if _db is None:
        from pymongo import MongoClient

        _client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        _db = _client[_session_db_name()]
    return _db


def fetch_access_token() -> str | None:
    """Return the access token written by kiteob OAuth, or None."""
    db = _get_db()
    if db is None:
        return None
    try:
        doc = db[COLLECTION].find_one({"key": DOC_KEY}, {"token": 1})
        token = doc.get("token") if doc else None
        if isinstance(token, str) and token.strip():
            return token.strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("kite_session_store.fetch_failed error=%s", exc)
    return None


def persist_access_token(token: str) -> None:
    """Write token to the kiteob-compatible MongoDB doc."""
    t = token.strip()
    if not t:
        return
    db = _get_db()
    if db is None:
        return
    try:
        db[COLLECTION].update_one(
            {"key": DOC_KEY},
            {"$set": {"key": DOC_KEY, "token": t, "updatedAt": datetime.now(timezone.utc)}},
            upsert=True,
        )
        log.info("kite_session_store.persisted db=%s", _session_db_name())
    except Exception as exc:  # noqa: BLE001
        log.warning("kite_session_store.persist_failed error=%s", exc)
