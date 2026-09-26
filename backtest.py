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


def run(candles_by_tf: dict[str, list[dict]], style: str, min_rr: float = 1.5, min_score: int = 55) -> dict:
    st = STYLES[style]
    tfs = needed_timeframes(style)
    analysed = [st["entry"], st["confirm"], st["bias"]]
    # A candle is usable only once it has closed (time is its open time).
    closes = {tf: [_parse(c["time"]) + timedelta(minutes=TF_MIN[tf]) for c in candles_by_tf[tf]] for tf in tfs}
    steps = [t for t in closes[st["entry"]] if all(bisect_right(closes[tf], t) >= WARMUP for tf in analysed)]
    if not steps:
        return {"error": "not enough history for this style"}

    m5, m5_close = candles_by_tf["5min"], closes["5min"]
    trades, open_trades, seen = [], [], set()
    prev = None
    for now in steps:
        # 1) advance open trades with the M5 candles that closed since the last step
        lo = bisect_right(m5_close, prev) if prev else bisect_right(m5_close, now)
        fresh = m5[lo:bisect_right(m5_close, now)]
        for t in open_trades:
            tracker.update(t, fresh, now)
        open_trades = [t for t in open_trades if t["status"] != "closed"]
        prev = now

        # 2) look for a new setup on the candles known at this moment
        view = {}
        for tf in tfs:
            k = bisect_right(closes[tf], now)
            view[tf] = candles_by_tf[tf][max(0, k - WINDOW):k]
        if not view["5min"]:
            continue
        market = analyze_market({tf: view[tf] for tf in analysed})
        market["levels"] = key_levels(view["1day"], view["5min"])
        setup = find_setup(style, market, sessions.current(now), min_rr)
        if not setup or setup["score"] < min_score or setup["key"] in seen:
            continue
        if any(t["direction"] == setup["direction"] for t in open_trades):
            continue
        seen.add(setup["key"])
        trade = tracker.new_trade(setup, {"confidence": 0, "votes": 0, "reports": []}, view["5min"][-1]["time"], now)
        trades.append(trade)
        open_trades.append(trade)

    closed = sorted((t for t in trades if t["status"] == "closed"), key=lambda t: t["closed_at"])
    s = stats(closed)
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
        "stats": s,
        "avg_r": round(sum(results) / len(results), 2) if results else 0.0,
        "max_dd": round(max_dd, 2),
        "profit_factor": round(gains / losses, 2) if losses else ("∞" if gains else 0),
        "trades": closed,
    }


async def fetch_history(api_key: str, symbol: str, style: str) -> dict[str, list[dict]]:
    async with httpx.AsyncClient() as client:
        return {tf: await fetch_candles(client, api_key, symbol, tf, outputsize=5000) for tf in needed_timeframes(style)}


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
