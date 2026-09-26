"""Technical indicators computed from OHLC candles (oldest candle first)."""


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average, aligned with `values` (None until warmed up)."""
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out: list[float | None] = [None] * (period - 1) + [prev]
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI of the latest candle."""
    if len(closes) <= period:
        return None
    diffs = [b - a for a, b in zip(closes, closes[1:])]
    avg_gain = sum(max(d, 0) for d in diffs[:period]) / period
    avg_loss = sum(max(-d, 0) for d in diffs[:period]) / period
    for d in diffs[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(candles: list[dict], period: int = 14) -> float | None:
    """Wilder's Average True Range of the latest candle."""
    if len(candles) <= period:
        return None
    trs = [
        max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
        for p, c in zip(candles, candles[1:])
    ]
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def macd(closes: list[float]) -> dict | None:
    """MACD(12, 26, 9) line, signal and histogram of the latest candle."""
    fast, slow = ema(closes, 12), ema(closes, 26)
    line = [f - s for f, s in zip(fast, slow) if f is not None and s is not None]
    signal = ema(line, 9)
    if not signal or signal[-1] is None:
        return None
    return {"line": line[-1], "signal": signal[-1], "histogram": line[-1] - signal[-1]}

