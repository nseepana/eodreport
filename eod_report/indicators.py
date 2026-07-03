"""Technical indicators for EOD enrichment."""

from __future__ import annotations

from dataclasses import dataclass


def round2(n: float) -> float:
    return round(n * 100) / 100


@dataclass
class PivotLevels:
    pivot: float
    s1: float
    s2: float
    r1: float
    r2: float


def classic_pivot(high: float, low: float, close: float) -> PivotLevels:
    p = (high + low + close) / 3
    return PivotLevels(
        pivot=round2(p),
        s1=round2(2 * p - high),
        s2=round2(p - (high - low)),
        r1=round2(2 * p - low),
        r2=round2(p + (high - low)),
    )


def ema_series(values: list[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    ema = sum(values[:period]) / period
    out.append(ema)
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
        out.append(ema)
    return out


def rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round2(100 - 100 / (1 + rs))


def pivot_signal(close: float, pv: PivotLevels) -> str:
    band = (pv.r2 - pv.s2) * 0.02
    if close >= pv.r2 + band:
        return "above_r2"
    if close >= pv.r2 - band:
        return "near_r2"
    if close > pv.r1 + band:
        return "between_r1_r2"
    if close >= pv.r1 - band:
        return "near_r1"
    if close > pv.pivot + band:
        return "above_pivot"
    if close >= pv.pivot - band:
        return "pivot_zone"
    if close > pv.s1 + band:
        return "below_pivot"
    if close >= pv.s1 - band:
        return "near_s1"
    if close > pv.s2 + band:
        return "between_s1_s2"
    if close >= pv.s2 - band:
        return "near_s2"
    return "below_s2"


def trend_from_emas(close: float, ema20: float | None, ema50: float | None) -> str:
    if ema20 is None or ema50 is None:
        return "indeterminate"
    if close > ema20 and ema20 > ema50:
        return "bullish (above 20/50 EMA, 20>50)"
    if close < ema20 and ema20 < ema50:
        return "bearish (below 20/50 EMA, 20<50)"
    if close > ema50:
        return "sideways-to-positive (above 50 EMA)"
    return "sideways-to-negative (below 50 EMA)"


def sector_bias(pct: float) -> str:
    if pct > 0.15:
        return "bullish"
    if pct < -0.15:
        return "bearish"
    return "neutral"
