"""Thin KiteConnect wrapper for EOD report market data (no kite-trader dependency)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from kiteconnect import KiteConnect

from eod_report.config import EodReportConfig

JsonDict = dict[str, Any]
JsonList = list[dict[str, Any]]


class EodKiteClient:
    def __init__(self, access_token: str, api_key: str | None = None) -> None:
        key = (api_key or EodReportConfig.from_env().kite_api_key or "").strip()
        self.kite = KiteConnect(api_key=key)
        self.kite.set_access_token(access_token)

    def get_ohlc(self, symbols: list[str]) -> JsonDict:
        return self.kite.ohlc(symbols)

    def instruments(self, exchange: str) -> JsonList:
        return self.kite.instruments(exchange)

    def get_historical_data(
        self,
        instrument_token: int,
        from_date: str,
        to_date: str,
        interval: str = "day",
    ) -> JsonList:
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        return self.kite.historical_data(
            instrument_token,
            datetime.strptime(from_date, "%Y-%m-%d"),
            to_dt,
            interval,
        )
