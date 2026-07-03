"""Perplexity chat/completions client for EOD report generation."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests

from eod_report.config import (
    EodReportConfig,
    PERPLEXITY_MARKET_DOMAINS,
    PERPLEXITY_PRICING,
)

log = logging.getLogger(__name__)

API_URL = "https://api.perplexity.ai/chat/completions"

HOST_BLOCKLIST = (
    "ticketmaster.com",
    "eventbrite.com",
    "facebook.com",
    "instagram.com",
    "pinterest.com",
    "reddit.com",
)

MARKET_KEYWORDS = re.compile(
    r"\b(nse|bse|nifty|sensex|bank\s*nifty|fii|dii|india|indian|stock|market|equity|"
    r"share|rupee|crude|brent|rbi|sebi|earnings|ipo|futures|options|expiry|macro|"
    r"fed|fomc|inflation|gdp|wpi|cpi|vix|midcap|smallcap|sector|gainer|loser|"
    r"trading|investor|index|bond|yield|forex|commodit)\b",
    re.IGNORECASE,
)


def _price_for(cfg: EodReportConfig) -> tuple[float, float]:
    if cfg.perplexity_input_price is not None and cfg.perplexity_output_price is not None:
        return cfg.perplexity_input_price, cfg.perplexity_output_price
    return PERPLEXITY_PRICING.get(cfg.perplexity_model, PERPLEXITY_PRICING["sonar-pro"])


def _host_of(url: str) -> str:
    try:
        return urlparse(url).hostname.replace("www.", "").lower() if urlparse(url).hostname else ""
    except Exception:  # noqa: BLE001
        return ""


def _host_allowed(host: str) -> bool:
    for d in PERPLEXITY_MARKET_DOMAINS:
        if host == d or host.endswith(f".{d}"):
            return True
    return False


def _is_plain_gov(host: str) -> bool:
    return bool(re.search(r"\.gov$", host, re.IGNORECASE))


def _is_relevant_source(source: dict[str, Any]) -> bool:
    url = (source.get("url") or "").strip()
    if not url:
        return False
    host = _host_of(url)
    title = source.get("title") or ""
    hay = f"{host} {url}".lower()
    if _is_plain_gov(host):
        return False
    if any(b in hay for b in HOST_BLOCKLIST):
        return False
    if re.search(r"/events?\b|/calendar\b", url, re.IGNORECASE):
        return False
    if _host_allowed(host):
        return True
    return bool(MARKET_KEYWORDS.search(f"{title} {url}"))


def filter_market_web_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for s in sources:
        if not _is_relevant_source(s):
            continue
        key = s.get("url", "").rstrip("/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _mdy(d: datetime) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def call_perplexity(
    cfg: EodReportConfig,
    *,
    system_prompt: str,
    user_content: str,
    session_date: str | None = None,
    next_session_date: str | None = None,
) -> dict[str, Any]:
    """Call Perplexity and return {text, cost, sources, finish_reason, error?}."""
    if not cfg.perplexity_configured():
        return {"error": "PERPLEXITY_API_KEY is not configured."}

    after_filter: str | None = None
    before_filter: str | None = None
    if session_date and re.match(r"^\d{4}-\d{2}-\d{2}$", session_date):
        base = datetime.strptime(session_date, "%Y-%m-%d")
        after = datetime.fromordinal(base.toordinal() - 1)
        before = datetime.fromordinal(base.toordinal() + 7)
        after_filter = _mdy(after)
        before_filter = _mdy(before)
    if next_session_date and re.match(r"^\d{4}-\d{2}-\d{2}$", next_session_date):
        after = datetime.strptime(session_date or next_session_date, "%Y-%m-%d")
        after = datetime.fromordinal(after.toordinal() - 1)
        before = datetime.strptime(next_session_date, "%Y-%m-%d")
        before = datetime.fromordinal(before.toordinal() + 7)
        after_filter = _mdy(after)
        before_filter = _mdy(before)

    body: dict[str, Any] = {
        "model": cfg.perplexity_model,
        "max_tokens": 8192,
        "temperature": 0,
        "web_search_options": {"search_context_size": "high"},
        "search_domain_filter": list(PERPLEXITY_MARKET_DOMAINS),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    if after_filter:
        body["search_after_date_filter"] = after_filter
    if before_filter:
        body["search_before_date_filter"] = before_filter

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.perplexity_api_key}",
    }
    try:
        r = requests.post(API_URL, headers=headers, json=body, timeout=180)
        data = r.json()
    except requests.RequestException as exc:
        return {"error": str(exc)}

    if not r.ok:
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message", str(err))
        else:
            msg = str(err) if err else f"Perplexity API error ({r.status_code})"
        return {"error": msg}

    choices = data.get("choices") or []
    text = (choices[0].get("message") or {}).get("content", "") if choices else ""
    finish_reason = choices[0].get("finish_reason") if choices else None

    usage = data.get("usage") or {}
    in_tok = int(usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or 0)
    in_price, out_price = _price_for(cfg)
    in_cost = (in_tok / 1_000_000) * in_price
    out_cost = (out_tok / 1_000_000) * out_price
    cost = {
        "model": cfg.perplexity_model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": int(usage.get("total_tokens") or in_tok + out_tok),
        "input_cost_usd": in_cost,
        "output_cost_usd": out_cost,
        "total_cost_usd": in_cost + out_cost,
    }

    raw_sources: list[dict[str, Any]] = []
    for s in data.get("search_results") or []:
        if isinstance(s, dict) and s.get("url"):
            raw_sources.append({
                "title": s.get("title") or s["url"],
                "url": s["url"],
                "date": s.get("date"),
            })
    if not raw_sources:
        for url in data.get("citations") or []:
            if url:
                raw_sources.append({"title": url, "url": url})

    return {
        "text": text,
        "cost": cost,
        "sources": filter_market_web_sources(raw_sources),
        "finish_reason": finish_reason,
    }
