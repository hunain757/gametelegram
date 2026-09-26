"""Engine-only backtest: replays history candle by candle (no look-ahead) through the same
setup finder and trade tracker the live bot uses, then reports win rate, R and drawdown.

    python backtest.py intraday        # or scalp / swing

The AI desk and news filter are not replayed (that would need thousands of AI calls), so the
live bot should do better than this on quality and worse on number of signals.
"""

import asyncio
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone

import httpx

import sessions
import tracker
from levels import key_levels
from market_data import fetch_candles
from setups import STYLES, analyze_market, find_setup
from storage import stats

TF_MIN = {"5min": 5, "15min": 15, "1h": 60, "4h": 240, "1day": 1440}
WINDOW = 300
WARMUP = 100


def _parse(t: str) -> datetime:
    return datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def needed_timeframes(style: str) -> list[str]:
    st = STYLES[style]
    return sorted({st["entry"], st["confirm"], st["bias"], "5min", "1day"}, key=TF_MIN.get)


def _track_tf(closes: dict, since) -> str:
    """Finest timeframe whose history already covers `since` (M5 only goes back a few weeks)."""
    for tf in ("5min", "15min", "1h", "4h"):
        if tf in closes and closes[tf] and closes[tf][0] <= since:
            return tf
    return "4h"


def _summary(style: str, steps: list, trades: list, open_trades: list) -> dict:
    closed = sorted((t for t in trades if t["status"] == "closed"), key=lambda t: t["closed_at"])
    results = [t["result_r"] for t in closed if t["outcome"] in ("win", "loss", "breakeven")]
    equity = peak = max_dd = 0.0
    for r in results:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    gains = sum(r for r in results if r > 0)
    losses = -sum(r for r in results if r < 0)
    return {
        "label": STYLES[style]["label"],
        "start": steps[0].strftime("%d %b %Y"),
        "end": steps[-1].strftime("%d %b %Y"),
        "steps": len(steps),
        "signals": len(trades),
        "still_open": len(open_trades),
        "stats": stats(closed),
        "avg_r": round(sum(results) / len(results), 2) if results else 0.0,
        "max_dd": round(max_dd, 2),
        "profit_factor": round(gains / losses, 2) if losses else ("∞" if gains else 0),
        "trades": closed,
    }


def run_many(candles_by_tf: dict[str, list[dict]], style: str, configs: list[dict]) -> list[dict]:
    """Replay once and evaluate several settings side by side (the market analysis is shared).

    Each config: {"min_score": int, "min_rr": float, "tp1_max_r": float | None}.
    """
    st = STYLES[style]
    tfs = [tf for tf in needed_timeframes(style) if candles_by_tf.get(tf)]
    for tf in ("15min", "1h"):  # finer candles for trade tracking when M5 history runs out
        if candles_by_tf.get(tf) and tf not in tfs:
            tfs.append(tf)
    analysed = [st["entry"], st["confirm"], st["bias"]]
    # A candle is usable only once it has closed (time is its open time).
    closes = {tf: [_parse(c["time"]) + timedelta(minutes=TF_MIN[tf]) for c in candles_by_tf[tf]] for tf in tfs}
    if any(tf not in closes for tf in analysed):
        return [{"error": "missing timeframe data"} for _ in configs]
    steps = [t for t in closes[st["entry"]] if all(bisect_right(closes[tf], t) >= WARMUP for tf in analysed)]
    if not steps:
        return [{"error": "not enough history for this style"} for _ in configs]

    accounts = [{"cfg": c, "trades": [], "open": [], "seen": set()} for c in configs]
    prev = None
    for now in steps:
        # 1) advance open trades with the candles that closed since the last step
        ttf = _track_tf(closes, prev or now)
        lo = bisect_right(closes[ttf], prev) if prev else bisect_right(closes[ttf], now)
        fresh = candles_by_tf[ttf][lo:bisect_right(closes[ttf], now)]
        for acc in accounts:
            for t in acc["open"]:
                tracker.update(t, fresh, now)
            acc["open"] = [t for t in acc["open"] if t["status"] != "closed"]
        prev = now

        # 2) market read on the candles known at this moment (shared by every config)
        view = {}
        for tf in tfs:
            k = bisect_right(closes[tf], now)
            view[tf] = candles_by_tf[tf][max(0, k - WINDOW):k]
        track_view = view.get(ttf) or []
        if not track_view:
            continue
        market = analyze_market({tf: view[tf] for tf in analysed})
        market["levels"] = key_levels(view.get("1day"), view.get("5min") or None)
        session = sessions.current(now)

        # 3) each config decides on its own
        for acc in accounts:
            c = acc["cfg"]
            setup = find_setup(style, market, session, c["min_rr"], c.get("tp1_max_r"))
            if not setup or setup["score"] < c["min_score"] or setup["key"] in acc["seen"]:
                continue
            if any(t["direction"] == setup["direction"] for t in acc["open"]):
                continue
            acc["seen"].add(setup["key"])
            trade = tracker.new_trade(setup, {"confidence": 0, "votes": 0, "reports": []}, track_view[-1]["time"], now)
            acc["trades"].append(trade)
            acc["open"].append(trade)

    return [{**_summary(style, steps, a["trades"], a["open"]), "config": a["cfg"]} for a in accounts]


def run(candles_by_tf: dict[str, list[dict]], style: str, min_rr: float = 1.5, min_score: int = 55,
        tp1_max_r: float | None = None) -> dict:
    return run_many(candles_by_tf, style, [{"min_score": min_score, "min_rr": min_rr, "tp1_max_r": tp1_max_r}])[0]


GRID = [{"min_score": s, "min_rr": r, "tp1_max_r": c}
        for s in (55, 65, 75) for r in (1.5, 2.0) for c in (None, 2.0)]


def optimize(candles_by_tf: dict[str, list[dict]], style: str, min_trades: int = 8) -> dict:
    """Try every setting in GRID on the same history and rank them by total R (then profit factor)."""
    results = run_many(candles_by_tf, style, GRID)
    if results and results[0].get("error"):
        return {"error": results[0]["error"]}
    rows = []
    for r in results:
        s = r["stats"]
        pf = r["profit_factor"]
        rows.append({"config": r["config"], "trades": s["trades"], "win_rate": s["win_rate"], "total_r": s["total_r"],
                     "max_dd": r["max_dd"], "profit_factor": pf if isinstance(pf, (int, float)) else 99.0})
    usable = [x for x in rows if x["trades"] >= min_trades]
    ranked = sorted(usable, key=lambda x: (x["total_r"], x["profit_factor"]), reverse=True)
    return {"label": STYLES[style]["label"], "style": style, "start": results[0]["start"], "end": results[0]["end"],
            "ranked": ranked, "too_few": len(rows) - len(usable), "min_trades": min_trades}


async def fetch_history(api_key: str, symbol: str, style: str) -> dict[str, list[dict]]:
    async with httpx.AsyncClient() as client:
        tfs = sorted(set(needed_timeframes(style)) | {"15min", "1h"}, key=TF_MIN.get)
        return {tf: await fetch_candles(client, api_key, symbol, tf, outputsize=5000) for tf in tfs}


def main():
    from config import load_config

    style = sys.argv[1] if len(sys.argv) > 1 else "intraday"
    if style not in STYLES:
        raise SystemExit(f"Style must be one of: {', '.join(STYLES)}")
    cfg = load_config()
    print(f"Downloading history for {style}…")
    data = asyncio.run(fetch_history(cfg.twelvedata_api_key, cfg.symbol, style))
    print("Replaying…")
    r = run(data, style, cfg.min_risk_reward, cfg.min_engine_score)
    if r.get("error"):
        raise SystemExit(r["error"])
    s = r["stats"]
    print(f"\n{r['label']}  {r['start']} → {r['end']}  ({r['steps']} scans)")
    print(f"Signals {r['signals']} | finished {s['trades']} | win rate {s['win_rate']}% | "
          f"total {s['total_r']}R | avg {r['avg_r']}R | max DD {r['max_dd']}R | PF {r['profit_factor']}")
    for t in r["trades"][-15:]:
        print(f"  {t['created_at'][:16]} {t['direction']:4} {t['entry']:>9.2f} SL {t['stop_loss']:>9.2f} "
              f"→ {t['outcome']:9} TP {t['stage']}/3  {t['result_r']:+.2f}R")


if __name__ == "__main__":
    main()
