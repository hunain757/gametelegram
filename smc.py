"""Smart Money Concepts on OHLC candles (oldest first).

Swings, market structure (BOS / CHoCH), order blocks, fair value gaps,
liquidity pools and sweeps, and premium / discount of the dealing range.
"""

from indicators import atr
from patterns import candle_patterns, double_tops_bottoms


def find_swings(candles: list[dict], left: int = 2, right: int = 2) -> list[dict]:
    """Fractal pivots: a high (low) that is the extreme of `left` candles before and `right` after."""
    swings = []
    for i in range(left, len(candles) - right):
        window = candles[i - left : i + right + 1]
        high, low = candles[i]["high"], candles[i]["low"]
        if high == max(c["high"] for c in window) and all(c["high"] < high for c in window[left + 1 :]):
            swings.append({"idx": i, "type": "high", "price": high, "time": candles[i]["time"]})
        if low == min(c["low"] for c in window) and all(c["low"] > low for c in window[left + 1 :]):
            swings.append({"idx": i, "type": "low", "price": low, "time": candles[i]["time"]})
    return swings


def market_structure(candles: list[dict], swings: list[dict], right: int = 2) -> dict:
    """Walk candles in order and record every close beyond the latest confirmed swing.

    The first break in a new direction is a CHoCH (change of character); further
    breaks in the same direction are BOS (break of structure).
    """
    trend = None
    events = []
    last_high = last_low = None
    pending = sorted(swings, key=lambda s: s["idx"])
    p = 0
    for i, c in enumerate(candles):
        while p < len(pending) and pending[p]["idx"] + right <= i:
            s = dict(pending[p])
            if s["type"] == "high":
                last_high = s
            else:
                last_low = s
            p += 1
        if last_high and not last_high.get("broken") and c["close"] > last_high["price"]:
            kind = "BOS" if trend == "bullish" else "CHoCH"
            events.append({"idx": i, "type": kind, "direction": "bullish", "level": last_high["price"],
                           "swing_idx": last_high["idx"], "time": c["time"], "body": abs(c["close"] - c["open"])})
            last_high["broken"] = True
            trend = "bullish"
        if last_low and not last_low.get("broken") and c["close"] < last_low["price"]:
            kind = "BOS" if trend == "bearish" else "CHoCH"
            events.append({"idx": i, "type": kind, "direction": "bearish", "level": last_low["price"],
                           "swing_idx": last_low["idx"], "time": c["time"], "body": abs(c["close"] - c["open"])})
            last_low["broken"] = True
            trend = "bearish"
    return {"trend": trend, "events": events}


def order_blocks(candles: list[dict], events: list[dict], max_blocks: int = 5,
                 breakers: list | None = None) -> list[dict]:
    """The last opposite candle before the move that broke structure; kept while not invalidated."""
    blocks = []
    for ev in events:
        seg = range(ev["swing_idx"], ev["idx"] + 1)
        if ev["direction"] == "bullish":
            origin = min(seg, key=lambda j: candles[j]["low"])
            opposite = lambda c: c["close"] < c["open"]  # noqa: E731
        else:
            origin = max(seg, key=lambda j: candles[j]["high"])
            opposite = lambda c: c["close"] > c["open"]  # noqa: E731
        ob_idx = next((j for j in range(origin, max(origin - 6, -1), -1) if opposite(candles[j])), origin)
        ob = candles[ob_idx]
        block = {"direction": ev["direction"], "top": ob["high"], "bottom": ob["low"], "idx": ob_idx,
                 "time": ob["time"], "created_by": ev["type"], "break_idx": ev["idx"], "touched": False}

        # Invalidate on a close through the block; note if price came back into it.
        valid = True
        broken_at = None
        for j in range(ev["idx"] + 1, len(candles)):
            c = candles[j]
            if ev["direction"] == "bullish":
                if c["close"] < block["bottom"]:
                    valid, broken_at = False, j
                    break
                if c["low"] <= block["top"]:
                    block["touched"] = True
            else:
                if c["close"] > block["top"]:
                    valid, broken_at = False, j
                    break
                if c["high"] >= block["bottom"]:
                    block["touched"] = True
        if valid and all(b["idx"] != ob_idx for b in blocks):
            blocks.append(block)
        elif broken_at is not None and breakers is not None:
            breakers.append(dict(block, broken_at=broken_at))
    return blocks[-max_blocks:]


def breaker_blocks(candles: list[dict], failed: list[dict], max_blocks: int = 4) -> list[dict]:
    """A failed order block flips sides: a broken bullish OB becomes bearish resistance and vice versa.

    Kept while price has not closed back through it after the break.
    """
    out = []
    for ob in failed:
        direction = "bearish" if ob["direction"] == "bullish" else "bullish"
        z = {"direction": direction, "top": ob["top"], "bottom": ob["bottom"], "idx": ob["broken_at"],
             "time": candles[ob["broken_at"]]["time"], "touched": False}
        valid = True
        for c in candles[ob["broken_at"] + 1:]:
            if direction == "bearish":
                if c["close"] > z["top"]:
                    valid = False
                    break
                z["touched"] = z["touched"] or c["high"] >= z["bottom"]
            else:
                if c["close"] < z["bottom"]:
                    valid = False
                    break
                z["touched"] = z["touched"] or c["low"] <= z["top"]
        if valid and all(o["idx"] != z["idx"] for o in out):
            out.append(z)
    return out[-max_blocks:]


def fair_value_gaps(candles: list[dict], min_size: float = 0.0, max_gaps: int = 6) -> list[dict]:
    """Three-candle imbalances that price has not fully filled yet."""
    gaps = []
    for k in range(2, len(candles)):
        a, c = candles[k - 2], candles[k]
        if c["low"] > a["high"] and c["low"] - a["high"] > min_size:
            gap = {"direction": "bullish", "top": c["low"], "bottom": a["high"], "idx": k - 1}
        elif c["high"] < a["low"] and a["low"] - c["high"] > min_size:
            gap = {"direction": "bearish", "top": a["low"], "bottom": c["high"], "idx": k - 1}
        else:
            continue
        gap["time"] = candles[k - 1]["time"]
        filled = False
        for later in candles[k + 1 :]:
            if gap["direction"] == "bullish" and later["low"] <= gap["bottom"]:
                filled = True
                break
            if gap["direction"] == "bearish" and later["high"] >= gap["top"]:
                filled = True
                break
        if not filled:
            gaps.append(gap)
    return gaps[-max_gaps:]


def liquidity(candles: list[dict], swings: list[dict], tolerance: float, sweep_lookback: int = 10) -> dict:
    """Resting liquidity (untaken swing highs/lows, equal highs/lows) and recent sweeps."""
    price = candles[-1]["close"]
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    def untaken(s):
        later = candles[s["idx"] + 1 :]
        if s["type"] == "high":
            return all(c["high"] <= s["price"] for c in later)
        return all(c["low"] >= s["price"] for c in later)

    buy_side = sorted({round(s["price"], 2) for s in highs if untaken(s) and s["price"] > price})
    sell_side = sorted({round(s["price"], 2) for s in lows if untaken(s) and s["price"] < price}, reverse=True)

    def equal_levels(points):
        levels = []
        for i, a in enumerate(points):
            for b in points[i + 1 :]:
                if abs(a["price"] - b["price"]) <= tolerance:
                    levels.append(round((a["price"] + b["price"]) / 2, 2))
        return sorted(set(levels))

    sweeps = []
    start = max(len(candles) - sweep_lookback, 0)
    for i in range(start, len(candles)):
        c = candles[i]
        for s in highs:
            if s["idx"] < i - 2 and c["high"] > s["price"] and c["close"] < s["price"] \
                    and all(x["high"] <= s["price"] for x in candles[s["idx"] + 1 : i]):
                sweeps.append({"direction": "bearish", "level": s["price"], "extreme": c["high"],
                               "idx": i, "time": c["time"], "side": "buy-side"})
        for s in lows:
            if s["idx"] < i - 2 and c["low"] < s["price"] and c["close"] > s["price"] \
                    and all(x["low"] >= s["price"] for x in candles[s["idx"] + 1 : i]):
                sweeps.append({"direction": "bullish", "level": s["price"], "extreme": c["low"],
                               "idx": i, "time": c["time"], "side": "sell-side"})

    return {
        "buy_side": buy_side[:5],
        "sell_side": sell_side[:5],
        "equal_highs": equal_levels(highs[-10:]),
        "equal_lows": equal_levels(lows[-10:]),
        "sweeps": sweeps[-4:],
    }


def dealing_range(candles: list[dict], lookback: int = 60) -> dict:
    """Premium / discount of the recent range: below 50% is discount (buy zone), above is premium."""
    window = candles[-lookback:]
    high, low = max(c["high"] for c in window), min(c["low"] for c in window)
    price = candles[-1]["close"]
    pos = (price - low) / (high - low) if high > low else 0.5
    zone = "discount" if pos < 0.45 else "premium" if pos > 0.55 else "equilibrium"
    return {"high": round(high, 2), "low": round(low, 2), "equilibrium": round((high + low) / 2, 2),
            "position": round(pos, 3), "zone": zone}


def analyze(candles: list[dict]) -> dict:
    """Full SMC read of one timeframe."""
    a = atr(candles) or 0.0
    swings = find_swings(candles)
    structure = market_structure(candles, swings)
    events = structure["events"]
    failed: list[dict] = []
    n = len(candles)
    return {
        "atr": a,
        "price": candles[-1]["close"],
        "trend": structure["trend"],
        "events": events[-6:],
        "last_event": events[-1] if events else None,
        "last_event_age": (n - 1 - events[-1]["idx"]) if events else None,
        "swings": swings[-12:],
        "order_blocks": order_blocks(candles, events, breakers=failed),
        "breakers": breaker_blocks(candles, failed),
        "fvgs": fair_value_gaps(candles, min_size=a * 0.1),
        "liquidity": liquidity(candles, swings, tolerance=a * 0.15),
        "range": dealing_range(candles),
        "patterns": candle_patterns(candles) + double_tops_bottoms(swings, tolerance=a * 0.3),
        "recent_candles": [{k: (round(c[k], 2) if k != "time" else c[k][5:16]) for k in ("time", "open", "high", "low", "close")}
                           for c in candles[-8:]],
        "recent": {"high": max(c["high"] for c in candles[-10:]), "low": min(c["low"] for c in candles[-10:])},
        "n": n,
    }
