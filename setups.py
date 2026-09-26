"""Turns the multi-timeframe SMC / volume read into concrete trade setups.

Each trading style uses three timeframes:
  bias    - higher timeframe trend we trade with
  confirm - middle timeframe that must not disagree
  entry   - timeframe where we look for the trigger (CHoCH/BOS or liquidity sweep)
            and a point of interest (order block / fair value gap) to enter from.
"""

import smc
import volume
from indicators import ema, macd, rsi
from levels import BUY_SIDE, SELL_SIDE, key_levels
from patterns import daily_range

STYLES = {
    "scalp": {"label": "⚡ Scalping", "entry": "5min", "confirm": "15min", "bias": "1h",
              "expiry_min": 60, "max_risk_atr": 3.0},
    "intraday": {"label": "📊 Intraday", "entry": "15min", "confirm": "1h", "bias": "4h",
                 "expiry_min": 240, "max_risk_atr": 3.0},
    "swing": {"label": "🌊 Swing", "entry": "1h", "confirm": "4h", "bias": "1day",
              "expiry_min": 1440, "max_risk_atr": 3.5},
}

TF_LABEL = {"5min": "M5", "15min": "M15", "1h": "H1", "4h": "H4", "1day": "D1"}


def analyze_market(candles_by_tf: dict[str, list[dict]], volume_by_tf: dict[str, list[dict]] | None = None) -> dict:
    """SMC + volume + indicator read for every timeframe."""
    volume_by_tf = volume_by_tf or {}
    market = {}
    for tf, candles in candles_by_tf.items():
        if len(candles) < 30:
            continue
        closes = [c["close"] for c in candles]
        m = macd(closes)
        market[tf] = {
            "price": closes[-1],
            "smc": smc.analyze(candles),
            "volume": volume.analyze(volume_by_tf.get(tf), xau_price=closes[-1]),
            "ind": {
                "ema20": ema(closes, 20)[-1],
                "ema50": ema(closes, 50)[-1],
                "ema200": ema(closes, 200)[-1],
                "rsi": rsi(closes),
                "macd_hist": m["histogram"] if m else None,
            },
        }
    market["levels"] = key_levels(candles_by_tf.get("1day"), candles_by_tf.get("5min"))
    market["adr"] = daily_range(candles_by_tf.get("1day"))
    return market


def _in_or_near(zone: dict, price: float, atr_v: float, bull: bool) -> bool:
    """Price is inside the zone, or just above a bullish zone / below a bearish one (retest pending)."""
    if zone["bottom"] <= price <= zone["top"]:
        return True
    if bull:
        return 0 < price - zone["top"] <= atr_v
    return 0 < zone["bottom"] - price <= atr_v


def pick_targets(entry: float, risk: float, levels: list[float], bull: bool, min_rr: float) -> list[dict]:
    """Three take-profits: at resting liquidity when there is some in range, else at fixed R multiples."""
    sign = 1 if bull else -1
    dists = sorted({round(sign * (lvl - entry), 2) for lvl in levels
                    if min_rr * risk <= sign * (lvl - entry) <= 12 * risk})
    out, prev = [], 0.0
    for fallback_r in (min_rr, max(min_rr + 1.0, 2.5), max(min_rr + 2.5, 4.0)):
        gap = 0.75 * risk if prev else 0.0
        at_liq = [d for d in dists if d >= prev + gap] if prev else dists
        if at_liq:
            d, source = at_liq[0], "liquidity"
        else:
            d, source = max(fallback_r * risk, prev + risk), "R-multiple"
        out.append({"price": round(entry + sign * d, 2), "rr": round(d / risk, 2), "source": source})
        prev = d
    return out


def validate_levels(direction: str, entry: float, sl: float, tps: list[float], price: float,
                    atr_v: float, min_rr: float, entry_type: str) -> tuple[bool, str]:
    bull = direction == "BUY"
    if bull and not (sl < entry < tps[0] <= tps[1] <= tps[2]):
        return False, "BUY levels out of order"
    if not bull and not (sl > entry > tps[0] >= tps[1] >= tps[2]):
        return False, "SELL levels out of order"
    risk = abs(entry - sl)
    if risk < 0.3 * atr_v:
        return False, "stop loss too tight"
    if abs(tps[0] - entry) / risk < min_rr - 1e-9:
        return False, f"TP1 below 1:{min_rr}"
    if entry_type == "MARKET" and abs(entry - price) > 0.5 * atr_v:
        return False, "market entry too far from price"
    if entry_type == "LIMIT":
        wrong_side = entry > price + 0.1 * atr_v if bull else entry < price - 0.1 * atr_v
        if wrong_side or abs(entry - price) > 2.5 * atr_v:
            return False, "limit entry in the wrong place"
    return True, "ok"


def find_setup(style: str, market: dict, session: dict, min_rr: float) -> dict | None:
    st = STYLES[style]
    if any(tf not in market for tf in (st["entry"], st["confirm"], st["bias"])):
        return None
    e, c, b = market[st["entry"]], market[st["confirm"]], market[st["bias"]]
    es, cs, bs = e["smc"], c["smc"], b["smc"]
    price, a = e["price"], es["atr"]
    if not a:
        return None
    key = market.get("levels", {})

    bias = bs["trend"] or cs["trend"]
    if bias is None:
        return None
    bull = bias == "bullish"
    want, against = ("bullish", "bearish") if bull else ("bearish", "bullish")
    if cs["trend"] == against:
        return None  # middle timeframe disagrees: stand aside

    score, conf = 0, []
    bias_ev = bs["last_event"]
    score += 20
    conf.append(f"{TF_LABEL[st['bias']]} {want} bias" + (f" ({bias_ev['type']})" if bias_ev else ""))
    if cs["trend"] == want:
        score += 15
        conf.append(f"{TF_LABEL[st['confirm']]} structure agrees")

    # Trigger on the entry timeframe: fresh structure shift and/or a liquidity sweep.
    ev = es["last_event"]
    struct_trigger = ev and ev["direction"] == want and es["last_event_age"] <= 20
    # A sweep only counts while price holds beyond its wick.
    sweeps = [s for s in es["liquidity"]["sweeps"] if s["direction"] == want
              and (price > s["extreme"] if bull else price < s["extreme"])]
    # Sweep of a key level (PDL/PWL/Asia low for buys, PDH/PWH/Asia high for sells) and reclaim.
    key_sweep = None
    for name in (SELL_SIDE if bull else BUY_SIDE):
        lvl = key.get(name)
        if lvl is None or abs(price - lvl) > 3 * a:
            continue
        if bull and es["recent"]["low"] < lvl < price:
            key_sweep = {"name": name, "level": lvl, "extreme": es["recent"]["low"]}
        elif not bull and price < lvl < es["recent"]["high"]:
            key_sweep = {"name": name, "level": lvl, "extreme": es["recent"]["high"]}
        if key_sweep:
            break
    if not struct_trigger and not sweeps and not key_sweep:
        return None
    if struct_trigger:
        score += 15
        conf.append(f"{TF_LABEL[st['entry']]} {ev['type']} {want} at {ev['level']:.2f}")
        if ev.get("body", 0) >= a:
            score += 5
            conf.append("Strong displacement candle")
    sweep = sweeps[-1] if sweeps else None
    if sweep:
        score += 15
        conf.append(f"{sweep['side'].capitalize()} liquidity swept at {sweep['level']:.2f}")
    if key_sweep:
        score += 10 if sweep else 15
        conf.append(f"{key_sweep['name']} ({key_sweep['level']:.2f}) swept and reclaimed")

    # Point of interest to enter from.
    pois = [dict(z, kind="Order Block") for z in es["order_blocks"] if z["direction"] == want]
    pois += [dict(z, kind="Fair Value Gap") for z in es["fvgs"] if z["direction"] == want]
    pois = [z for z in pois if _in_or_near(z, price, a, bull)]
    if not pois:
        return None
    poi = min(pois, key=lambda z: abs(price - (z["top"] if bull else z["bottom"])))
    kinds = {z["kind"] for z in pois}
    score += 10 if "Order Block" in kinds else 5
    if len(kinds) == 2:
        score += 5
    conf.append(f"{TF_LABEL[st['entry']]} {' + '.join(sorted(kinds))} at {poi['bottom']:.2f}–{poi['top']:.2f}")

    if poi["bottom"] <= price <= poi["top"]:
        entry_type, entry = "MARKET", round(price, 2)
    else:
        entry_type, entry = "LIMIT", round(poi["top"] if bull else poi["bottom"], 2)

    # Stop loss beyond the POI and any sweep wick, plus an ATR buffer.
    wicks = [w["extreme"] for w in (sweep, key_sweep) if w]
    if bull:
        sl = min([poi["bottom"]] + wicks) - 0.25 * a
    else:
        sl = max([poi["top"]] + wicks) + 0.25 * a
    # Resting liquidity just beyond the stop attracts stop hunts: put the stop past it instead.
    pools = [key[n] for n in (SELL_SIDE if bull else BUY_SIDE) if n in key]
    for tf in (st["entry"], st["confirm"], st["bias"]):
        liq = market[tf]["smc"]["liquidity"]
        pools += liq["sell_side"] + liq["equal_lows"] if bull else liq["buy_side"] + liq["equal_highs"]
    for lvl in sorted(pools, reverse=not bull):
        if bull and sl - 0.6 * a <= lvl <= sl + 0.1 * a and lvl < entry:
            sl = lvl - 0.15 * a
        elif not bull and sl - 0.1 * a <= lvl <= sl + 0.6 * a and lvl > entry:
            sl = lvl + 0.15 * a

    risk = abs(entry - sl)
    if risk < 0.5 * a:
        risk = 0.5 * a
        sl = entry - risk if bull else entry + risk
    if risk > st["max_risk_atr"] * a:
        return None
    sl = round(sl, 2)

    # Take profits at the next resting liquidity above (buy) / below (sell).
    targets = [key[n] for n in (BUY_SIDE if bull else SELL_SIDE) if n in key]
    for tf in (st["entry"], st["confirm"], st["bias"]):
        liq = market[tf]["smc"]["liquidity"]
        targets += liq["buy_side"] + liq["equal_highs"] if bull else liq["sell_side"] + liq["equal_lows"]
    vol = e["volume"]
    if vol.get("available"):
        targets += [vol["poc"], vol["value_area_high"] if bull else vol["value_area_low"]]
    tps = pick_targets(entry, risk, targets, bull, min_rr)

    # Clear path: an opposing order block / FVG before TP1 would stall the move.
    opposing = [z for z in es["order_blocks"] + cs["order_blocks"] if z["direction"] == against]
    opposing += [z for z in cs["fvgs"] if z["direction"] == against]
    edges = []
    for z in opposing:
        if z["bottom"] <= entry <= z["top"]:
            return None  # entering inside an opposing zone
        edge = z["bottom"] if bull else z["top"]
        if (bull and entry < edge < tps[0]["price"]) or (not bull and tps[0]["price"] < edge < entry):
            edges.append(edge)
    if edges:
        edge = min(edges) if bull else max(edges)
        tp1 = edge - 0.1 * a if bull else edge + 0.1 * a
        if abs(tp1 - entry) < min_rr * risk:
            return None  # not enough room before the opposing zone
        tps[0] = {"price": round(tp1, 2), "rr": round(abs(tp1 - entry) / risk, 2), "source": "before opposing zone"}
        conf.append(f"TP1 placed before opposing zone at {edge:.2f}")
    else:
        score += 5
        conf.append("Clear path to TP1")

    # Premium / discount.
    zone = es["range"]["zone"]
    if (bull and zone == "discount") or (not bull and zone == "premium"):
        score += 10
        conf.append(f"Price in {zone} ({es['range']['position'] * 100:.0f}% of range)")
    elif zone != "equilibrium":
        score -= 10

    # Volume confirmation.
    if vol.get("available"):
        if vol["pressure"] == ("buyers" if bull else "sellers"):
            score += 5
            conf.append(f"Volume pressure: {vol['pressure']} (delta {vol['delta']:+.2f})")
        if vol["relative_volume_last_closed"] >= 1.3:
            score += 5
            conf.append(f"Volume spike {vol['relative_volume_last_closed']}x average")

    # Momentum sanity.
    r = e["ind"]["rsi"]
    if r is not None:
        if (bull and r > 75) or (not bull and r < 25):
            score -= 10
        elif (bull and 40 <= r <= 65) or (not bull and 35 <= r <= 60):
            score += 5

    # Price-action confirmation on the entry timeframe.
    pa = [x for x in es.get("patterns", []) if x["direction"] == want and x.get("age", 0) <= 2]
    if pa:
        score += 5
        conf.append(f"{TF_LABEL[st['entry']]} {pa[-1]['name']}")
    if any(x["direction"] == against and x.get("age", 0) <= 1 for x in es.get("patterns", [])):
        score -= 5

    # Most of the average daily range already used: less room left today.
    adr = market.get("adr") or {}
    if adr.get("used_pct") and adr["used_pct"] >= 120 and style != "swing":
        score -= 10

    if session.get("killzone"):
        score += 5
        conf.append(f"{session['killzone']} killzone")

    ok, _ = validate_levels("BUY" if bull else "SELL", entry, sl, [t["price"] for t in tps], price, a,
                            min_rr, entry_type)
    if not ok:
        return None

    return {
        "style": style,
        "style_label": st["label"],
        "direction": "BUY" if bull else "SELL",
        "entry_type": entry_type,
        "entry": entry,
        "stop_loss": sl,
        "tps": tps,
        "price": round(price, 2),
        "atr": round(a, 2),
        "score": max(0, min(score, 100)),
        "confluences": conf,
        "poi": {"kind": poi["kind"], "top": poi["top"], "bottom": poi["bottom"], "time": poi["time"]},
        "key": f"{style}:{'BUY' if bull else 'SELL'}:{poi['kind']}:{poi['time']}",
        "expiry_min": st["expiry_min"],
        "timeframes": {"entry": st["entry"], "confirm": st["confirm"], "bias": st["bias"]},
    }
