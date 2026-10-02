"""A board of classic trading strategies that each give an independent opinion on a setup.

Every strategy looks at the same candles-derived market read and says whether it AGREES with the setup's
direction, is AGAINST it, or is NEUTRAL (its conditions are not in play). The strategy desk agents read
these results; the dashboard shows them; the desk refuses a signal when more strategies are against it
than for it.
"""

from setups import TF_LABEL

GROUPS = {"trend": "Trend following", "breakout": "Breakout", "reversion": "Mean reversion",
          "smc": "ICT / SMC", "momentum": "Momentum timing", "volume": "Volume"}


# Strategy families that do not apply in a regime (their votes are shown but not counted).
NOT_IN = {"trending": ("reversion",), "ranging": ("trend", "breakout"), "compressed": ("trend", "reversion"),
          "volatile": ("reversion",), "transition": ()}


def _v(x, *path):
    for p in path:
        if not isinstance(x, dict):
            return None
        x = x.get(p)
    return x


def evaluate(market: dict, direction: str, timeframes: dict, entry: float | None = None) -> dict:
    """Run every strategy for a BUY/SELL on the setup's entry / confirm / bias timeframes."""
    bull = direction == "BUY"
    e_tf, c_tf, b_tf = timeframes.get("entry"), timeframes.get("confirm"), timeframes.get("bias")
    e, c = market.get(e_tf) or {}, market.get(c_tf) or {}
    ei, ci = e.get("ind") or {}, c.get("ind") or {}
    price = e.get("price")
    atr_v = _v(e, "smc", "atr") or 0
    entry = entry if entry is not None else price
    out = []

    def add(name, group, verdict, detail, tf=e_tf):
        out.append({"name": name, "group": group, "verdict": verdict, "detail": detail, "tf": TF_LABEL.get(tf, tf)})

    def side(ok_bull, ok_bear):
        """AGREES when the condition for our side holds, AGAINST when the opposite side's holds."""
        mine, theirs = (ok_bull, ok_bear) if bull else (ok_bear, ok_bull)
        return "agrees" if mine else "against" if theirs else "neutral"

    # --- trend following ---
    e20, e50, e200 = ci.get("ema20"), ci.get("ema50"), ci.get("ema200")
    if None not in (e20, e50, e200) and price:
        up, dn = e20 > e50 > e200, e20 < e50 < e200
        near = atr_v and min(abs(price - e20), abs(price - e50)) <= 1.5 * atr_v
        v = side(up and near, dn and near) if (up or dn) else "neutral"
        if (up and not bull or dn and bull):
            v = "against"
        add("EMA 20/50/200 pullback", "trend", v,
            f"EMA20 {e20:.2f} / EMA50 {e50:.2f} / EMA200 {e200:.2f}, price {price:.2f}"
            + (" near the EMAs" if near else " far from the EMAs"), c_tf)
    st, mh = _v(ei, "supertrend", "direction"), ei.get("macd_hist")
    if st and mh is not None:
        add("Supertrend + MACD", "trend", side(st == "bullish" and mh > 0, st == "bearish" and mh < 0),
            f"Supertrend {st}, MACD histogram {mh:.2f}")
    ich = ci.get("ichimoku")
    if ich:
        add("Ichimoku cloud", "trend",
            side(ich["position"] == "above cloud" and ich["tk"] == "bullish",
                 ich["position"] == "below cloud" and ich["tk"] == "bearish"),
            f"price {ich['position']}, Tenkan/Kijun {ich['tk']}, cloud {ich['cloud']}", c_tf)
    adx = ci.get("adx")
    if adx:
        strong = adx["adx"] >= 22
        add("ADX / DMI trend", "trend",
            side(strong and adx["plus_di"] > adx["minus_di"], strong and adx["minus_di"] > adx["plus_di"]),
            f"ADX {adx['adx']:.1f}, +DI {adx['plus_di']:.1f} / -DI {adx['minus_di']:.1f}"
            + ("" if strong else " (weak trend)"), c_tf)
    ha = ei.get("heikin_ashi")
    if ha:
        add("Heikin-Ashi trend", "trend", side(ha == "bullish", ha == "bearish"), f"last 3 Heikin-Ashi candles {ha}")
    trends = [(TF_LABEL.get(tf, tf), _v(market.get(tf), "smc", "trend")) for tf in ("5min", "15min", "1h", "4h", "1day")
              if market.get(tf)]
    if trends:
        ups = sum(t == "bullish" for _, t in trends)
        dns = sum(t == "bearish" for _, t in trends)
        add("Multi-timeframe alignment", "trend", side(ups >= 3 and ups > dns, dns >= 3 and dns > ups),
            ", ".join(f"{k} {t or 'ranging'}" for k, t in trends), b_tf)

    # --- breakout ---
    dc = ei.get("donchian")
    if dc:
        add("Donchian 20 breakout", "breakout", side(dc["breakout"] == "up", dc["breakout"] == "down"),
            f"channel {dc['lower']:.2f}-{dc['upper']:.2f}, breakout: {dc['breakout'] or 'none'}")
    sq = ei.get("squeeze")
    if sq and mh is not None:
        v = side(sq["fired"] and mh > 0, sq["fired"] and mh < 0) if sq["fired"] else "neutral"
        add("Volatility squeeze", "breakout", v,
            "squeeze just fired" if sq["fired"] else "squeeze building (breakout loading)" if sq["on"] else "no squeeze")

    # --- mean reversion ---
    r, bb = ei.get("rsi"), ei.get("bollinger")
    if r is not None and bb:
        pos = bb["position"]
        add("RSI + Bollinger reversion", "reversion",
            side(r < 35 and pos < 0.2, r > 65 and pos > 0.8)
            if not (bull and r > 72 and pos > 0.95 or not bull and r < 28 and pos < 0.05) else "against",
            f"RSI {r:.1f}, Bollinger position {pos:.2f} (0 = lower band, 1 = upper)")
    vw = ei.get("vwap")
    if vw and price and atr_v:
        dist = (price - vw) / atr_v
        add("VWAP bias", "reversion", side(-0.5 <= dist <= 2.5, -2.5 <= dist <= 0.5) if abs(dist) <= 2.5 else
            side(dist < -2.5, dist > 2.5), f"price {price:.2f} vs VWAP {vw:.2f} ({dist:+.1f} ATR)")
    div = ei.get("rsi_divergence")
    add("RSI divergence", "reversion", side(div == "bullish", div == "bearish"), f"divergence: {div or 'none'}")

    # --- ICT / SMC ---
    rng, fib = _v(e, "smc", "range"), e.get("fib")
    if rng and fib and entry:
        if bull:
            ote = fib["0.786"] - 0.2 * atr_v <= entry <= fib["0.618"] + 0.2 * atr_v
        else:  # for sells the OTE band sits 62-79% up from the low
            top_ote = rng["low"] + 0.786 * (rng["high"] - rng["low"])
            bot_ote = rng["low"] + 0.618 * (rng["high"] - rng["low"])
            ote = bot_ote - 0.2 * atr_v <= entry <= top_ote + 0.2 * atr_v
        add("ICT optimal trade entry (62-79%)", "smc", "agrees" if ote else "neutral",
            f"range {rng['low']}-{rng['high']}, entry {entry:.2f}" + (" inside OTE" if ote else " outside OTE"))
        add("Premium / discount", "smc", side(rng["zone"] == "discount", rng["zone"] == "premium"),
            f"price in {rng['zone']} ({rng['position'] * 100:.0f}% of the range)")

    # --- momentum timing ---
    stc = ei.get("stochastic")
    if stc:
        add("Stochastic timing", "momentum",
            side(stc["k"] < 30 and stc["k"] > stc["d"] or 30 <= stc["k"] <= 70 and stc["k"] > stc["d"],
                 stc["k"] > 70 and stc["k"] < stc["d"] or 30 <= stc["k"] <= 70 and stc["k"] < stc["d"]),
            f"%K {stc['k']:.1f} / %D {stc['d']:.1f}")
    cc, wr = ei.get("cci"), ei.get("williams_r")
    if cc is not None and wr is not None:
        exhausted = bull and cc > 200 or not bull and cc < -200
        add("CCI + Williams %R", "momentum",
            "against" if exhausted else side(cc > 0 and wr > -50, cc < 0 and wr < -50),
            f"CCI {cc:.0f}, Williams %R {wr:.0f}" + (" (exhausted)" if exhausted else ""))

    # --- volume ---
    obv = ei.get("obv")
    if obv:
        add("On-balance volume", "volume", side(obv == "rising", obv == "falling"), f"OBV {obv}")
    press = _v(e, "volume", "pressure")
    if press:
        add("Buy/sell pressure (delta)", "volume", side("buy" in str(press).lower(), "sell" in str(press).lower()),
            f"volume pressure: {press}")

    # Regime filter: a strategy only counts in the market conditions it was designed for.
    reg = (c.get("regime") or e.get("regime") or {}).get("regime", "transition")
    for x in out:
        x["applicable"] = x["group"] not in NOT_IN.get(reg, ())
    used = [x for x in out if x["applicable"]]
    agrees = sum(x["verdict"] == "agrees" for x in used)
    against = sum(x["verdict"] == "against" for x in used)
    return {"direction": direction, "regime": reg, "results": out, "agrees": agrees, "against": against,
            "neutral": len(used) - agrees - against, "ignored": len(out) - len(used),
            "score": round(100 * agrees / (agrees + against)) if agrees + against else 50,
            "groups": {g: {"label": GROUPS[g], "applicable": g not in NOT_IN.get(reg, ()),
                           "agrees": sum(x["verdict"] == "agrees" for x in used if x["group"] == g),
                           "against": sum(x["verdict"] == "against" for x in used if x["group"] == g)}
                       for g in GROUPS if any(x["group"] == g for x in out)}}


def brief(board: dict, groups: tuple[str, ...] | None = None) -> list[str]:
    """One line per strategy, optionally only some groups."""
    return [f"{x['name']} [{x['tf']}]: {x['verdict'].upper()}"
            + ("" if x.get("applicable", True) else f" (not counted in a {board.get('regime')} market)")
            + f" - {x['detail']}"
            for x in board.get("results", []) if not groups or x["group"] in groups]
