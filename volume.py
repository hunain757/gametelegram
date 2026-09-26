"""Volume analysis: relative volume, buy/sell pressure and a volume profile (POC / value area).

Spot gold (XAU/USD) has no central volume, so the bot uses PAXG/USDT (tokenized gold,
1 PAXG = 1 oz) from Binance as the volume source and shifts its price levels onto XAU/USD.
"""


def has_volume(candles: list[dict] | None) -> bool:
    return bool(candles) and sum(c.get("volume") or 0 for c in candles[-50:]) > 0


def analyze(candles: list[dict] | None, xau_price: float | None = None, lookback: int = 20) -> dict:
    if not has_volume(candles):
        return {"available": False}

    vols = [c["volume"] for c in candles]
    avg = sum(vols[-lookback - 1 : -1]) / max(len(vols[-lookback - 1 : -1]), 1)
    rvol = vols[-1] / avg if avg else 0.0
    # Last *closed* candle is a fairer read than the forming one.
    rvol_closed = vols[-2] / avg if avg and len(vols) > 1 else 0.0

    recent = candles[-lookback:]
    up = sum(c["volume"] for c in recent if c["close"] >= c["open"])
    down = sum(c["volume"] for c in recent if c["close"] < c["open"])
    delta = (up - down) / (up + down) if up + down else 0.0

    # Biggest-volume candle recently: where the "smart money" traded.
    spike = max(recent, key=lambda c: c["volume"])

    profile = volume_profile(candles[-120:])
    shift = (xau_price - candles[-1]["close"]) if xau_price else 0.0

    return {
        "available": True,
        "relative_volume": round(rvol, 2),
        "relative_volume_last_closed": round(rvol_closed, 2),
        "delta": round(delta, 2),
        "pressure": "buyers" if delta > 0.15 else "sellers" if delta < -0.15 else "balanced",
        "spike_candle": {"time": spike["time"], "direction": "up" if spike["close"] >= spike["open"] else "down",
                         "x_avg": round(spike["volume"] / avg, 2) if avg else None},
        "poc": round(profile["poc"] + shift, 2),
        "value_area_high": round(profile["vah"] + shift, 2),
        "value_area_low": round(profile["val"] + shift, 2),
    }


def volume_profile(candles: list[dict], bins: int = 40) -> dict:
    low = min(c["low"] for c in candles)
    high = max(c["high"] for c in candles)
    step = (high - low) / bins or 1.0
    buckets = [0.0] * bins
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        buckets[min(int((typical - low) / step), bins - 1)] += c["volume"]

    poc_i = max(range(bins), key=lambda i: buckets[i])
    total, area = sum(buckets), buckets[poc_i]
    lo = hi = poc_i
    while total and area / total < 0.7 and (lo > 0 or hi < bins - 1):
        below = buckets[lo - 1] if lo > 0 else -1
        above = buckets[hi + 1] if hi < bins - 1 else -1
        if above >= below:
            hi += 1
            area += buckets[hi]
        else:
            lo -= 1
            area += buckets[lo]
    mid = lambda i: low + (i + 0.5) * step  # noqa: E731
    return {"poc": mid(poc_i), "vah": low + (hi + 1) * step, "val": low + lo * step}
