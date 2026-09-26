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


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI for every candle (None until warmed up)."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    diffs = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(d, 0) for d in diffs[:period]) / period
    loss = sum(max(-d, 0) for d in diffs[:period]) / period
    out[period] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    for i, d in enumerate(diffs[period:], period + 1):
        gain = (gain * (period - 1) + max(d, 0)) / period
        loss = (loss * (period - 1) + max(-d, 0)) / period
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def stochastic(candles: list[dict], period: int = 14, smooth: int = 3) -> dict | None:
    """Stochastic oscillator %K / %D (0-100)."""
    if len(candles) < period + smooth:
        return None
    ks = []
    for i in range(len(candles) - smooth - 2, len(candles)):
        w = candles[i - period + 1: i + 1]
        hi, lo = max(c["high"] for c in w), min(c["low"] for c in w)
        ks.append(100 * (candles[i]["close"] - lo) / (hi - lo) if hi > lo else 50.0)
    k = sum(ks[-smooth:]) / smooth
    d = sum(sum(ks[j - smooth + 1: j + 1]) / smooth for j in range(len(ks) - smooth, len(ks))) / smooth
    return {"k": k, "d": d}


def cci(candles: list[dict], period: int = 20) -> float | None:
    """Commodity Channel Index: > +100 strong up-move / overbought, < -100 strong down-move / oversold."""
    if len(candles) < period:
        return None
    tp = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles[-period:]]
    mean = sum(tp) / period
    dev = sum(abs(x - mean) for x in tp) / period
    return (tp[-1] - mean) / (0.015 * dev) if dev else 0.0


def williams_r(candles: list[dict], period: int = 14) -> float | None:
    """Williams %R (-100..0): above -20 overbought, below -80 oversold."""
    if len(candles) < period:
        return None
    w = candles[-period:]
    hi, lo = max(c["high"] for c in w), min(c["low"] for c in w)
    return -100 * (hi - candles[-1]["close"]) / (hi - lo) if hi > lo else -50.0


def donchian(candles: list[dict], period: int = 20) -> dict | None:
    """Donchian channel of the previous `period` candles and whether the last close broke out of it."""
    if len(candles) < period + 1:
        return None
    w = candles[-period - 1:-1]
    hi, lo = max(c["high"] for c in w), min(c["low"] for c in w)
    close = candles[-1]["close"]
    return {"upper": hi, "lower": lo, "breakout": "up" if close > hi else "down" if close < lo else None}


def keltner(candles: list[dict], period: int = 20, mult: float = 1.5) -> dict | None:
    closes = [c["close"] for c in candles]
    mid, a = ema(closes, period)[-1], atr(candles, period)
    if mid is None or a is None:
        return None
    return {"upper": mid + mult * a, "mid": mid, "lower": mid - mult * a}


def squeeze(candles: list[dict]) -> dict | None:
    """TTM-style squeeze: Bollinger Bands inside the Keltner Channel = volatility compressed, a breakout
    is loading. 'fired' = the squeeze just released on the last candle."""
    closes = [c["close"] for c in candles]
    if len(candles) < 22:
        return None

    def on(n):
        bb, kc = bollinger(closes[:n] if n else closes), keltner(candles[:n] if n else candles)
        return bool(bb and kc and bb["upper"] < kc["upper"] and bb["lower"] > kc["lower"])
    now, before = on(0), on(-1)
    return {"on": now, "fired": before and not now}


def ichimoku(candles: list[dict]) -> dict | None:
    """Ichimoku (9, 26, 52): Tenkan, Kijun, the cloud under the current candle and where price is."""
    if len(candles) < 78:
        return None

    def mid(w):
        return (max(c["high"] for c in w) + min(c["low"] for c in w)) / 2
    tenkan, kijun = mid(candles[-9:]), mid(candles[-26:])
    past = candles[:-26]  # the cloud printed 26 candles ago is the one under price now
    span_a = (mid(past[-9:]) + mid(past[-26:])) / 2
    span_b = mid(past[-52:])
    top, bottom = max(span_a, span_b), min(span_a, span_b)
    close = candles[-1]["close"]
    return {"tenkan": tenkan, "kijun": kijun, "cloud_top": top, "cloud_bottom": bottom,
            "position": "above cloud" if close > top else "below cloud" if close < bottom else "inside cloud",
            "tk": "bullish" if tenkan > kijun else "bearish" if tenkan < kijun else "flat",
            "cloud": "bullish" if span_a > span_b else "bearish"}


def heikin_ashi_trend(candles: list[dict], n: int = 3) -> str | None:
    """'bullish' / 'bearish' when the last `n` Heikin-Ashi candles share a colour, else 'mixed'."""
    if len(candles) < n + 10:
        return None
    ha_open = (candles[0]["open"] + candles[0]["close"]) / 2
    colours = []
    for c in candles[1:]:
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        colours.append(ha_close >= ha_open)
        ha_open = (ha_open + ha_close) / 2
    last = colours[-n:]
    return "bullish" if all(last) else "bearish" if not any(last) else "mixed"


def rsi_divergence(candles: list[dict], lookback: int = 40) -> str | None:
    """Regular divergence between the last two swing highs / lows and RSI:
    'bearish' = higher high in price, lower high in RSI; 'bullish' = lower low in price, higher low in RSI."""
    if len(candles) < lookback + 20:
        return None
    rs = rsi_series([c["close"] for c in candles])
    start = len(candles) - lookback
    highs = [i for i in range(start + 2, len(candles) - 2)
             if candles[i]["high"] >= max(c["high"] for c in candles[i - 2:i + 3])]
    lows = [i for i in range(start + 2, len(candles) - 2)
            if candles[i]["low"] <= min(c["low"] for c in candles[i - 2:i + 3])]
    if len(highs) >= 2 and rs[highs[-1]] and rs[highs[-2]]:
        a, b = highs[-2], highs[-1]
        if candles[b]["high"] > candles[a]["high"] and rs[b] < rs[a] - 2:
            return "bearish"
    if len(lows) >= 2 and rs[lows[-1]] and rs[lows[-2]]:
        a, b = lows[-2], lows[-1]
        if candles[b]["low"] < candles[a]["low"] and rs[b] > rs[a] + 2:
            return "bullish"
    return None


def obv_trend(volumes: list[dict] | None, period: int = 20) -> str | None:
    """On-balance-volume slope over the last `period` candles ('rising' / 'falling' / 'flat')."""
    if (not volumes or len(volumes) < period + 1 or not any(v.get("volume") for v in volumes[-period:])
            or any("close" not in v for v in volumes[-period - 1:])):
        return None
    obv, series = 0.0, []
    for p, c in zip(volumes[-period - 1:], volumes[-period:]):
        obv += (c.get("volume") or 0) * (1 if c["close"] > p["close"] else -1 if c["close"] < p["close"] else 0)
        series.append(obv)
    total = sum(v.get("volume") or 0 for v in volumes[-period:]) or 1
    slope = (series[-1] - series[0]) / total
    return "rising" if slope > 0.1 else "falling" if slope < -0.1 else "flat"


def pivots(daily: list[dict] | None) -> dict:
    """Classic floor pivots from the previous daily candle."""
    if not daily or len(daily) < 2:
        return {}
    p = daily[-2]
    pp = (p["high"] + p["low"] + p["close"]) / 3
    rng = p["high"] - p["low"]
    return {"P": pp, "R1": 2 * pp - p["low"], "S1": 2 * pp - p["high"], "R2": pp + rng, "S2": pp - rng}


def fib_levels(high: float, low: float) -> dict:
    """Retracement levels of a range, incl. the ICT optimal trade entry (OTE) band 62-79%."""
    r = high - low
    return {"0.382": high - 0.382 * r, "0.5": high - 0.5 * r, "0.618": high - 0.618 * r,
            "0.705": high - 0.705 * r, "0.786": high - 0.786 * r}
