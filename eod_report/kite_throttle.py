"""Serialize Kite historical_data calls to stay under the ~3 req/s cap."""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_last_call = 0.0
# Kite Connect historical API: burst >3/s triggers NetworkException "Too many requests".
_MIN_INTERVAL_S = 0.36


def pause_before_kite_historical() -> None:
    global _last_call
    with _lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_S - (now - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
