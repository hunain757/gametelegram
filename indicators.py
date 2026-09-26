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



def _wilder(values: list[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)
    out: list[float | None] = [None] * (period - 1)
    prev = sum(values[:period]) / period
    out.append(prev)
    for v in values[period:]:
        prev = (prev * (period - 1) + v) / period
        out.append(prev)
    return out


def adx(candles: list[dict], period: int = 14) -> dict | None:
    """Average Directional Index: trend strength (>25 strong) and direction (+DI vs -DI)."""
    if len(candles) < period * 2 + 1:
        return None
    tr, plus, minus = [], [], []
    for p, c in zip(candles, candles[1:]):
        up, down = c["high"] - p["high"], p["low"] - c["low"]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        tr.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    atr_s, p_s, m_s = _wilder(tr, period), _wilder(plus, period), _wilder(minus, period)
    dx = []
    for a, pp, mm in zip(atr_s, p_s, m_s):
        if a is None or not a:
            continue
        pdi, mdi = 100 * pp / a, 100 * mm / a
        dx.append((100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0, pdi, mdi))
    adx_s = _wilder([d[0] for d in dx], period)
    if not adx_s or adx_s[-1] is None:
        return None
    return {"adx": adx_s[-1], "plus_di": dx[-1][1], "minus_di": dx[-1][2]}


def supertrend(candles: list[dict], period: int = 10, mult: float = 3.0) -> dict | None:
    """Supertrend direction ('bullish'/'bearish') and its line value."""
    if len(candles) < period + 2:
        return None
    trs = [candles[0]["high"] - candles[0]["low"]] + [
        max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
        for p, c in zip(candles, candles[1:])]
    atr_s = _wilder(trs, period)
    upper = lower = None
    trend = "bullish"
    for i, c in enumerate(candles):
        a = atr_s[i]
        if a is None:
            continue
        mid = (c["high"] + c["low"]) / 2
        bu, bl = mid + mult * a, mid - mult * a
        prev_close = candles[i - 1]["close"]
        upper = bu if upper is None or bu < upper or prev_close > upper else upper
        lower = bl if lower is None or bl > lower or prev_close < lower else lower
        if c["close"] > upper:
            trend = "bullish"
        elif c["close"] < lower:
            trend = "bearish"
    return {"direction": trend, "line": lower if trend == "bullish" else upper}


def stoch_rsi(closes: list[float], period: int = 14, k: int = 3) -> float | None:
    """Stochastic RSI %K (0-100): <20 oversold, >80 overbought."""
    need = period + k - 1
    if len(closes) < period * 2 + need:
        return None
    rs = [rsi(closes[: len(closes) - j], period) for j in range(need - 1, -1, -1)]
    ks = []
    for i in range(period - 1, need):
        window = rs[i - period + 1: i + 1]
        lo, hi = min(window), max(window)
        ks.append(100 * (rs[i] - lo) / (hi - lo) if hi > lo else 50.0)
    return sum(ks) / len(ks)


def bollinger(closes: list[float], period: int = 20, mult: float = 2.0) -> dict | None:
    if len(closes) < period:
        return None
    w = closes[-period:]
    mid = sum(w) / period
    sd = (sum((x - mid) ** 2 for x in w) / period) ** 0.5
    return {"upper": mid + mult * sd, "mid": mid, "lower": mid - mult * sd,
            "width_pct": 100 * 2 * mult * sd / mid if mid else 0.0,
            "position": (closes[-1] - (mid - mult * sd)) / (2 * mult * sd) if sd else 0.5}


def vwap(candles: list[dict], volumes: list[dict] | None = None) -> float | None:
    """Session VWAP from the start of the latest UTC day (uses volume if given, else equal weights)."""
    if not candles:
        return None
    day = candles[-1]["time"][:10]
    todays = [c for c in candles if c["time"][:10] == day]
    vol = {v["time"]: v.get("volume") or 0 for v in (volumes or [])}
    num = den = 0.0
    for c in todays:
        w = vol.get(c["time"]) or 1.0
        num += (c["high"] + c["low"] + c["close"]) / 3 * w
        den += w
    return num / den if den else None
