"""Price action: candlestick patterns on the latest candles, double tops/bottoms, and daily range (ADR)."""


def _body(c):
    return abs(c["close"] - c["open"])


def _range(c):
    return max(c["high"] - c["low"], 1e-9)


def candle_patterns(candles: list[dict], lookback: int = 3) -> list[dict]:
    """Classic reversal / continuation candles among the last `lookback` closed candles."""
    found = []
    n = len(candles)
    for i in range(max(2, n - lookback), n):
        c, p, pp = candles[i], candles[i - 1], candles[i - 2]
        body, rng = _body(c), _range(c)
        upper = c["high"] - max(c["open"], c["close"])
        lower = min(c["open"], c["close"]) - c["low"]
        bull, pbull = c["close"] > c["open"], p["close"] > p["open"]
        age = n - 1 - i

        if bull and not pbull and c["close"] >= p["open"] and c["open"] <= p["close"] and body > _body(p):
            found.append({"name": "Bullish Engulfing", "direction": "bullish", "age": age})
        if not bull and pbull and c["close"] <= p["open"] and c["open"] >= p["close"] and body > _body(p):
            found.append({"name": "Bearish Engulfing", "direction": "bearish", "age": age})
        if lower >= 2 * body and lower >= 0.55 * rng and upper <= 0.25 * rng:
            found.append({"name": "Bullish Pin Bar (hammer)", "direction": "bullish", "age": age})
        if upper >= 2 * body and upper >= 0.55 * rng and lower <= 0.25 * rng:
            found.append({"name": "Bearish Pin Bar (shooting star)", "direction": "bearish", "age": age})
        if c["high"] <= p["high"] and c["low"] >= p["low"]:
            found.append({"name": "Inside Bar", "direction": "neutral", "age": age})
        if body <= 0.1 * rng:
            found.append({"name": "Doji", "direction": "neutral", "age": age})
        small_mid = _body(p) <= 0.35 * _range(p)
        if pp["close"] < pp["open"] and small_mid and bull and c["close"] > (pp["open"] + pp["close"]) / 2:
            found.append({"name": "Morning Star", "direction": "bullish", "age": age})
        if pp["close"] > pp["open"] and small_mid and not bull and c["close"] < (pp["open"] + pp["close"]) / 2:
            found.append({"name": "Evening Star", "direction": "bearish", "age": age})
    return found


def double_tops_bottoms(swings: list[dict], tolerance: float) -> list[dict]:
    """Two latest swing highs (lows) at about the same price."""
    out = []
    highs = [s for s in swings if s["type"] == "high"][-2:]
    lows = [s for s in swings if s["type"] == "low"][-2:]
    if len(highs) == 2 and abs(highs[0]["price"] - highs[1]["price"]) <= tolerance:
        out.append({"name": "Double Top", "direction": "bearish", "level": round(max(h["price"] for h in highs), 2)})
    if len(lows) == 2 and abs(lows[0]["price"] - lows[1]["price"]) <= tolerance:
        out.append({"name": "Double Bottom", "direction": "bullish", "level": round(min(x["price"] for x in lows), 2)})
    return out


def daily_range(daily: list[dict] | None, days: int = 14) -> dict:
    """Average daily range and how much of it today has already used."""
    if not daily or len(daily) < 3:
        return {}
    past = daily[-days - 1:-1]
    adr = sum(c["high"] - c["low"] for c in past) / len(past)
    today = daily[-1]["high"] - daily[-1]["low"]
    return {"adr": round(adr, 2), "today_range": round(today, 2), "used_pct": round(100 * today / adr) if adr else None}
