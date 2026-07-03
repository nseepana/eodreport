"""Environment for the standalone EOD report cron job."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


@dataclass(frozen=True)
class EodReportConfig:
    perplexity_api_key: str
    perplexity_model: str
    perplexity_input_price: float | None
    perplexity_output_price: float | None
    mongodb_uri: str
    mongodb_db: str
    kite_api_key: str
    kite_access_token: str

    @classmethod
    def from_env(cls) -> EodReportConfig:
        env = os.environ
        in_p = env.get("PERPLEXITY_INPUT_PRICE", "").strip()
        out_p = env.get("PERPLEXITY_OUTPUT_PRICE", "").strip()
        return cls(
            perplexity_api_key=_strip_quotes(env.get("PERPLEXITY_API_KEY", "")),
            perplexity_model=env.get("PERPLEXITY_MODEL", "sonar-pro").strip() or "sonar-pro",
            perplexity_input_price=float(in_p) if in_p else None,
            perplexity_output_price=float(out_p) if out_p else None,
            mongodb_uri=env.get("MONGODB_URI", "").strip(),
            mongodb_db=(
                env.get("EOD_REPORT_MONGODB_DB", "").strip()
                or env.get("KITE_SESSION_MONGODB_DB", "").strip()
                or "zerodha"
            ),
            kite_api_key=env.get("KITE_API_KEY", "").strip(),
            kite_access_token=env.get("KITE_ACCESS_TOKEN", "").strip(),
        )

    def perplexity_configured(self) -> bool:
        return bool(self.perplexity_api_key)


PERPLEXITY_PRICING = {
    "sonar": (1.0, 1.0),
    "sonar-pro": (3.0, 15.0),
    "sonar-reasoning": (1.0, 5.0),
    "sonar-reasoning-pro": (2.0, 8.0),
    "sonar-deep-research": (2.0, 8.0),
}

PERPLEXITY_MARKET_DOMAINS = [
    "economictimes.indiatimes.com",
    "moneycontrol.com",
    "livemint.com",
    "business-standard.com",
    "financialexpress.com",
    "ndtvprofit.com",
    "thehindubusinessline.com",
    "nseindia.com",
    "bseindia.com",
    "reuters.com",
    "bloomberg.com",
    "cnbc.com",
    "investing.com",
    "rbi.org.in",
    "sebi.gov.in",
    "capitalmind.in",
    "tickertape.in",
    "tradingeconomics.com",
    "tradingview.com",
    "marketwatch.com",
]

SECTOR_INDEX_NAMES = {
    "Nifty Fin Services": "NIFTY FINANCIAL SERVICES",
    "Nifty Bank": "NIFTY BANK",
    "Nifty IT": "NIFTY IT",
    "Nifty Auto": "NIFTY AUTO",
    "Nifty Pharma": "NIFTY PHARMA",
    "Nifty FMCG": "NIFTY FMCG",
    "Nifty Metal": "NIFTY METAL",
    "Nifty Realty": "NIFTY REALTY",
    "Nifty PSU Bank": "NIFTY PSU BANK",
    "Nifty Energy": "NIFTY ENERGY",
}
