"""Follows every signal on live M5 candles: entry fill, TP1/TP2/TP3, stop loss, expiry.

Rules (conservative):
  * a limit order fills when price trades back to the entry; it expires if not filled in time,
    and is cancelled if price reaches TP1 without filling first
  * if one candle touches both the stop and a target, the stop is assumed to have hit first
  * after TP1 the stop moves to breakeven, after TP2 it moves to TP1
  * results are booked as 1/3 of the position closed at each TP (realized R, not "max R reached")
"""

import secrets
from datetime import datetime, timedelta, timezone

MAX_ACTIVE_DAYS = 7


def pips(diff: float) -> float:
    """Gold: 1 pip = $0.10."""
    return round(diff * 10, 1)


def new_trade(setup: dict, verdict: dict, candle_time: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    levels = verdict.get("levels") or {"entry": setup["entry"], "stop_loss": setup["stop_loss"],
                                       "tps": [t["price"] for t in setup["tps"]]}
    entry, sl = round(levels["entry"], 2), round(levels["stop_loss"], 2)
    tps = [round(p, 2) for p in levels["tps"]]
    risk = abs(entry - sl)
    active = setup["entry_type"] == "MARKET"
    return {
        "id": secrets.token_hex(4),
        "style": setup["style"],
        "style_label": setup["style_label"],
        "direction": setup["direction"],
        "entry_type": setup["entry_type"],
        "entry": entry,
        "stop_loss": sl,
        "initial_sl": sl,
        "tps": tps,
        "rr": [round(abs(tp - entry) / risk, 2) for tp in tps],
        "status": "active" if active else "pending",
        "stage": 0,
        "outcome": None,
        "result_r": None,
        "created_at": now.isoformat(),
        "filled_at": now.isoformat() if active else None,
        "closed_at": None,
        "expires_at": (now + timedelta(minutes=setup["expiry_min"])).isoformat(),
        "created_candle": candle_time,
        "last_checked": candle_time,
        "price_at_signal": setup["price"],
        "score": setup["score"],
        "confluences": setup["confluences"],
        "confidence": verdict.get("confidence", 0),
        "votes": verdict.get("votes", 0),
        "headline": verdict.get("headline", ""),
        "reason": verdict.get("reason", ""),
        "reports": verdict.get("reports", []),
        "audit": verdict.get("audit"),
        "engine_only": verdict.get("engine_only", False),
        "timeframes": setup["timeframes"],
        "messages": {},
        "events": [],
    }


def realized_r(trade: dict, exit_price: float | None = None) -> float:
    """Realized R with the standard plan: close 1/3 at each TP; the rest exits at `exit_price`."""
    k = trade["stage"]
    booked = sum(trade["rr"][:k]) / 3
    if k == 3 or exit_price is None:
        return booked
    risk = abs(trade["entry"] - trade["initial_sl"])
    move = (exit_price - trade["entry"]) if trade["direction"] == "BUY" else (trade["entry"] - exit_price)
    return booked + (3 - k) / 3 * move / risk


def _close(trade: dict, outcome: str, result_r: float, now: datetime):
    trade["status"] = "closed"
    trade["outcome"] = outcome
    trade["result_r"] = round(result_r, 2)
    trade["closed_at"] = now.isoformat()


def update(trade: dict, candles: list[dict], now: datetime | None = None) -> list[dict]:
    """Advance one trade with new M5 candles. Returns the events that happened."""
    now = now or datetime.now(timezone.utc)
    events = []
    bull = trade["direction"] == "BUY"

    def crossed(c, level, favorable):
        """Did the candle reach `level`? favorable=True means in the trade's profit direction."""
        if bull == favorable:
            return c["high"] >= level
        return c["low"] <= level

    for c in candles:
        if trade["status"] == "closed":
            break
        if c["time"] <= trade["created_candle"] or c["time"] < trade["last_checked"]:
            continue
        trade["last_checked"] = c["time"]

        just_filled = False
        if trade["status"] == "pending":
            if crossed(c, trade["entry"], favorable=False):
                trade["status"] = "active"
                trade["filled_at"] = now.isoformat()
                just_filled = True
                events.append({"kind": "filled", "price": trade["entry"]})
            elif crossed(c, trade["tps"][0], favorable=True):
                _close(trade, "cancelled", 0, now)
                events.append({"kind": "cancelled"})
                break
            else:
                continue

        # Active: stop first (conservative), then targets.
        if crossed(c, trade["stop_loss"], favorable=False):
            if trade["stage"] == 0:
                _close(trade, "loss", -1, now)
                events.append({"kind": "sl", "price": trade["stop_loss"]})
            else:
                _close(trade, "win", realized_r(trade, trade["stop_loss"]), now)
                events.append({"kind": "protected_stop", "price": trade["stop_loss"], "stage": trade["stage"]})
            break
        if just_filled:
            continue  # the target may have printed before the fill inside this candle
        while trade["stage"] < 3 and crossed(c, trade["tps"][trade["stage"]], favorable=True):
            n = trade["stage"]
            trade["stage"] += 1
            events.append({"kind": "tp", "n": n + 1, "price": trade["tps"][n], "rr": trade["rr"][n]})
            if trade["stage"] == 1:
                trade["stop_loss"] = trade["entry"]
                events[-1]["new_sl"] = trade["entry"]
            elif trade["stage"] == 2:
                trade["stop_loss"] = trade["tps"][0]
                events[-1]["new_sl"] = trade["tps"][0]
        if trade["stage"] == 3:
            _close(trade, "win", realized_r(trade), now)
            break

    if trade["status"] == "pending" and now >= datetime.fromisoformat(trade["expires_at"]):
        _close(trade, "expired", 0, now)
        events.append({"kind": "expired"})
    elif trade["status"] == "active" and trade["filled_at"] and \
            now - datetime.fromisoformat(trade["filled_at"]) > timedelta(days=MAX_ACTIVE_DAYS):
        last = candles[-1]["close"] if candles else trade["entry"]
        r = realized_r(trade, last)
        _close(trade, "win" if trade["stage"] else ("loss" if r < 0 else "breakeven"), r, now)
        events.append({"kind": "timeout", "price": last})

    trade["events"].extend({**e, "at": now.isoformat()} for e in events)
    return events
