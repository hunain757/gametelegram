"""AI trading desk: 26 Gemini agents in a 5-stage pipeline. Every agent has exactly one job, sees only the data
for that job, and passes its finding on - every hand-over is shown on the local dashboard.

  Stage 1  18 analysts in 3 desks study the setup in parallel
             Technical desk (9)  structure, liquidity, order blocks, FVGs, volume, candles, momentum, trend, volatility
             Strategy desk  (5)  multi-timeframe, ICT/Fibonacci, key levels & pivots, trend-following, breakout/reversion
             Macro desk     (4)  economic calendar, world events, central banks & dollar, intermarket & sentiment
  Stage 2  3 desk leads check their members' evidence, challenge doubtful members (debate) and give one desk verdict
  Stage 3  3 verifiers cross-check the desks: confluence, risk, devil's advocate
  Stage 4  the Head Trader reads everything and decides TAKE/SKIP with final levels
  Stage 5  the Signal Auditor checks the final signal before it is sent (can veto)

With 2 API keys the work is split 13 / 13 (see TradingDesk._make_plan).
"""

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx
from google import genai
from google.genai import types

import strategies
from monitor import Monitor
from setups import TF_LABEL, validate_levels

log = logging.getLogger("goldbot.agents")

DESKS = {
    "tech": {"name": "Technical desk", "icon": "TECH"},
    "strategy": {"name": "Strategy desk", "icon": "STRAT"},
    "macro": {"name": "Macro & news desk", "icon": "MACRO"},
}

# Each analyst: its job ("focus"), the inputs it receives (shown on the dashboard) and the exact data slice.
#   fields  per-timeframe keys of the market read      ind     which indicators (per timeframe)
#   extras  other inputs: key_levels, adr, pivots, strategies:<groups>, calendar, headlines:<category>, intermarket
ANALYSTS = [
    # ---------------- technical desk ----------------
    {"key": "structure", "desk": "tech", "name": "Market Structure Analyst", "icon": "MS",
     "focus": "Market structure on every timeframe: trend, BOS vs CHoCH, whether the higher timeframes support this "
              "direction and whether the entry-timeframe shift is a real break or noise inside a range.",
     "fields": ("price", "trend", "regime", "structure_events", "range"), "extras": ()},
    {"key": "liquidity", "desk": "tech", "name": "Liquidity & Sweep Hunter", "icon": "LQ",
     "focus": "Liquidity: which pools (swing highs/lows, equal highs/lows, PDH/PDL, PWH/PWL, Asia range) were swept "
              "before this move, which still rest as targets, and whether the stop sits where liquidity will be hunted.",
     "fields": ("price", "atr", "buy_side_liquidity", "sell_side_liquidity", "equal_highs", "equal_lows",
                "recent_sweeps"), "extras": ("key_levels",)},
    {"key": "orderblocks", "desk": "tech", "name": "Order Block & Breaker Specialist", "icon": "OB",
     "focus": "Order blocks and breaker blocks: is the entry zone a fresh, untested OB or a valid breaker, did it "
              "cause a real break of structure, is an opposing OB/breaker between entry and TP1?",
     "fields": ("price", "atr", "order_blocks", "breaker_blocks", "structure_events"), "extras": ()},
    {"key": "imbalance", "desk": "tech", "name": "FVG & Imbalance Analyst", "icon": "FVG",
     "focus": "Fair value gaps: is an unfilled FVG supporting the entry, are opposing FVGs in the path to the "
              "targets, and did price displace (leave imbalance) in the trade direction?",
     "fields": ("price", "atr", "fvgs"), "extras": ()},
    {"key": "volume", "desk": "tech", "name": "Volume Profile & Order Flow Analyst", "icon": "VOL",
     "focus": "Volume (Binance: PAXG/USDT for gold, spot volume for crypto): relative volume, delta (buy/sell "
              "pressure), volume bubbles (institutional candles), POC / value area and OBV. Does volume confirm the "
              "move or show absorption against it? If volume data is unavailable say so and vote SKIP.",
     "fields": ("price", "volume"), "ind": ("obv",), "extras": ()},
    {"key": "price_action", "desk": "tech", "name": "Price Action & Pattern Reader", "icon": "PA",
     "focus": "Read the candles: recent candles on each timeframe, candlestick patterns (engulfing, pin bars, "
              "stars, inside bars, dojis), double tops/bottoms, rejection wicks. Is there real rejection from the "
              "entry zone or is price slicing through it?",
     "fields": ("price", "recent_candles", "candle_patterns"), "extras": ()},
    {"key": "momentum", "desk": "tech", "name": "Momentum Oscillator Analyst", "icon": "MOM",
     "focus": "Momentum oscillators: RSI, RSI divergence, MACD histogram, Stochastic, Stochastic RSI, CCI and "
              "Williams %R on each timeframe. Is momentum turning in the trade direction, or overbought/oversold "
              "exhaustion / divergence against it?",
     "fields": ("price",), "ind": ("rsi", "rsi_divergence", "macd_hist", "stochastic", "stoch_rsi", "cci",
                                   "williams_r"), "extras": ()},
    {"key": "trend", "desk": "tech", "name": "Trend Indicator Analyst", "icon": "TRD",
     "focus": "Trend indicators: EMA 20/50/200 stack, ADX with +DI/-DI, Supertrend, Ichimoku (cloud, Tenkan/Kijun), "
              "Heikin-Ashi and VWAP. Is there a real trend in the trade direction and how strong is it?",
     "fields": ("price",), "ind": ("ema20", "ema50", "ema200", "adx", "supertrend", "ichimoku", "heikin_ashi",
                                   "vwap"), "extras": ()},
    {"key": "volatility", "desk": "tech", "name": "Volatility Analyst", "icon": "VLT",
     "focus": "Volatility: ATR, Bollinger Bands (width, position), TTM squeeze, Donchian channel and how much of the "
              "average daily range is used. Is there enough room to reach the targets, or is the market too "
              "compressed / already over-extended?",
     "fields": ("price", "atr", "regime"), "ind": ("bollinger", "bb_width_rank", "squeeze", "donchian"),
     "extras": ("adr",)},
    # ---------------- strategy desk ----------------
    {"key": "mtf", "desk": "strategy", "name": "Multi-Timeframe Analyst", "icon": "MTF",
     "focus": "Top-down alignment D1 → H4 → H1 → M15 → M5: does every timeframe from the bias down to the entry "
              "agree with this direction? Name the exact timeframe that disagrees, if any.",
     "fields": ("price", "trend", "structure_events"), "ind": ("ema50", "ema200"),
     "extras": ("strategies:trend",)},
    {"key": "ict", "desk": "strategy", "name": "ICT & Fibonacci Specialist", "icon": "ICT",
     "focus": "ICT model: premium/discount of the dealing range, Fibonacci OTE zone (62-79%), killzone timing, "
              "Asian range and the power-of-three (accumulation → manipulation → distribution). Is the entry at a "
              "textbook ICT location and time?",
     "fields": ("price", "range", "fib"), "extras": ("key_levels", "strategies:smc")},
    {"key": "levels", "desk": "strategy", "name": "Key Levels & Pivot Analyst", "icon": "LVL",
     "focus": "Horizontal levels: PDH/PDL, PWH/PWL, daily/weekly open, floor pivots (P, R1/R2, S1/S2), round "
              "numbers and Fibonacci levels. Does a level support the entry, and is a level blocking the way to TP1?",
     "fields": ("price", "atr", "fib"), "extras": ("key_levels", "pivots")},
    {"key": "trend_follow", "desk": "strategy", "name": "Trend-Following Strategist", "icon": "TF",
     "focus": "Run the trend-following strategies (EMA pullback, Supertrend+MACD, Ichimoku, ADX/DMI, Heikin-Ashi, "
              "multi-timeframe) on this setup. How many AGREE vs are AGAINST, and is this a with-trend trade?",
     "fields": ("price",), "extras": ("strategies:trend",)},
    {"key": "breakout_rev", "desk": "strategy", "name": "Breakout & Reversion Strategist", "icon": "B/R",
     "focus": "Run the breakout (Donchian, squeeze), mean-reversion (RSI+Bollinger, VWAP, divergence), momentum-"
              "timing and volume strategies. Is this a good breakout or a good reversion entry - or a chase that "
              "these strategies warn against?",
     "fields": ("price",), "extras": ("strategies:breakout,reversion,momentum,volume",)},
    # ---------------- macro & news desk ----------------
    {"key": "calendar", "desk": "macro", "name": "Economic Calendar Analyst", "icon": "CAL",
     "focus": "The economic calendar only: which high/medium-impact USD events (CPI, NFP, FOMC, PCE, GDP, jobless "
              "claims…) fall inside this trade's lifetime, and how close they are. A red event within the trade "
              "window = SKIP. If no event threatens the trade, vote TAKE with a score of 60-70.",
     "fields": (), "extras": ("calendar",)},
    {"key": "world", "desk": "macro", "name": "Geopolitics & World Events Analyst", "icon": "GEO",
     "focus": "What is happening in the world right now: wars, conflicts, sanctions, tariffs, elections, oil and "
              "crises in the headlines. Would these events push this instrument in the trade direction (e.g. "
              "risk-off = gold up) or against it? If no relevant headline exists, vote TAKE with a score of 55-65.",
     "fields": (), "extras": ("headlines:world",)},
    {"key": "macro", "desk": "macro", "name": "Central Banks & Dollar Analyst", "icon": "CB",
     "focus": "Central banks and the US dollar: Fed/FOMC tone, inflation and jobs data, yields, rate-cut/hike "
              "expectations and dollar strength in the headlines. A stronger dollar and higher yields weigh on gold "
              "and bitcoin. Does the macro backdrop support this direction? No relevant headline = TAKE 55-65.",
     "fields": (), "extras": ("headlines:macro",)},
    {"key": "intermarket", "desk": "macro", "name": "Intermarket & Sentiment Analyst", "icon": "IMK",
     "focus": "Other markets and sentiment: the other instrument's trend and move today, the gold/bitcoin "
              "correlation, and this instrument's own news flow (gold or crypto headlines). Is money flowing with "
              "this trade or against it?",
     "fields": ("price", "trend"), "extras": ("intermarket", "headlines:instrument")},
]

LEADS = [
    {"key": "tech_lead", "desk": "tech", "name": "Technical Desk Lead", "icon": "TL",
     "focus": "Check the 9 technical analysts' evidence against the chart data and give the technical verdict."},
    {"key": "strategy_lead", "desk": "strategy", "name": "Strategy Desk Lead", "icon": "SL",
     "focus": "Check the 5 strategy analysts against the strategy board and levels and give the strategy verdict."},
    {"key": "macro_lead", "desk": "macro", "name": "Macro & News Desk Lead", "icon": "ML",
     "focus": "Check the 4 macro/news analysts against the calendar, headlines and other markets and give the "
              "fundamental verdict."},
]

VERIFIERS = [
    {"key": "confluence", "name": "Confluence Verifier", "icon": "CNF",
     "focus": "Cross-check the three desks against each other AND against the market data: do technical, strategy "
              "and macro agree, did any desk claim something the data does not show, which contradiction matters most?"},
    {"key": "risk", "name": "Risk Manager", "icon": "RSK",
     "focus": "Verify the stop loss (beyond invalidation, outside obvious stop hunts), risk/reward of each target, "
              "volatility, news risk inside the trade window and whether a limit entry can fill. Protect capital first."},
    {"key": "devil", "name": "Devil's Advocate", "icon": "DEV",
     "focus": "Attack the trade with everything the desks found: the strongest reasons it will FAIL (trap, fake "
              "breakout, counter-trend, liquidity that will be taken against it, news). Vote TAKE only if you "
              "honestly cannot find a serious flaw."},
]

for _a in ANALYSTS:
    _a["lead"] = next(lead["key"] for lead in LEADS if lead["desk"] == _a["desk"])
    _a.setdefault("ind", None)

SPECIALISTS = ANALYSTS + VERIFIERS
HEAD = {"key": "head", "name": "Head Trader", "icon": "HT",
        "focus": "Reads the 3 desk verdicts, the 3 verifiers, the strategy board and the track record → TAKE/SKIP, "
                 "confidence and final levels."}
AUDITOR = {"key": "auditor", "name": "Signal Auditor", "icon": "AUD",
           "focus": "Final consistency check of the signal before it is sent – can veto."}
ALL_AGENTS = ANALYSTS + LEADS + VERIFIERS + [HEAD, AUDITOR]
PIPELINE = [("Stage 1 · 18 analysts", ANALYSTS), ("Stage 2 · Desk leads", LEADS), ("Stage 3 · Verifiers", VERIFIERS),
            ("Stage 4 · Decision", [HEAD]), ("Stage 5 · Final check", [AUDITOR])]


def links() -> dict[str, dict]:
    """Who each agent receives information from and sends it to (the dashboard draws these)."""
    out = {}
    for a in ANALYSTS:
        out[a["key"]] = {"from": ["engine"], "to": [a["lead"]]}
    for lead in LEADS:
        members = [a["key"] for a in ANALYSTS if a["desk"] == lead["desk"]]
        out[lead["key"]] = {"from": members, "to": [v["key"] for v in VERIFIERS] + ["head"]}
    for v in VERIFIERS:
        out[v["key"]] = {"from": [lead["key"] for lead in LEADS], "to": ["head"]}
    out["head"] = {"from": [lead["key"] for lead in LEADS] + [v["key"] for v in VERIFIERS], "to": ["auditor"]}
    out["auditor"] = {"from": ["head"], "to": ["telegram"]}
    return out


def inputs(agent: dict) -> list[str]:
    """Human-readable list of the data an analyst receives (for the dashboard)."""
    names = {"price": "price", "atr": "ATR", "trend": "trend", "structure_events": "BOS/CHoCH events",
             "range": "dealing range", "buy_side_liquidity": "buy-side liquidity", "sell_side_liquidity":
             "sell-side liquidity", "equal_highs": "equal highs", "equal_lows": "equal lows", "recent_sweeps":
             "sweeps", "order_blocks": "order blocks", "breaker_blocks": "breaker blocks", "fvgs": "FVGs",
             "volume": "volume/delta/POC/bubbles", "recent_candles": "last candles", "candle_patterns": "patterns",
             "fib": "Fibonacci"}
    out = [names.get(f, f) for f in agent.get("fields", ()) if f != "price"]
    out += list(agent.get("ind") or ())
    for x in agent.get("extras", ()):
        out.append({"key_levels": "PDH/PDL/PWH/PWL/Asia", "adr": "ADR", "pivots": "floor pivots",
                    "calendar": "economic calendar", "intermarket": "other markets + correlation"}.get(x) or
                   x.replace("strategies:", "strategy board: ").replace("headlines:", "headlines: "))
    return out


ANALYST_PROMPT = """You are the {name} of the {desk} on a professional {instrument} trading desk.
26 agents work on this trade and each has exactly ONE job. YOUR ONLY JOB:
{focus}

STRICT RULES:
1. Judge ONLY your job. Other specialists cover everything else - never comment on their areas.
2. Use ONLY the data below. Every evidence item must quote an exact number, level, time or headline from it,
   so your desk lead can verify it. Never invent prices, events or news.
3. If your data is missing or does not clearly support a {direction}, vote SKIP with a score of 40 or less
   and say exactly what is missing or against it.
4. Score honestly: 80-100 = your data strongly supports the {direction}, 60-79 = supports it, below 60 = weak.
5. Your report goes to the {lead}, who checks your evidence and challenges you if it is wrong.

Setup proposed by the engine:
{setup}

Time / session: {session}

YOUR DATA:
{data}

Reply with JSON only:
{{"vote": "TAKE" or "SKIP", "score": 0-100, "summary": "one short sentence about YOUR job only",
  "evidence": ["2-3 exact data points you used, e.g. 'H1 bearish BOS @ 2617.89'"],
  "risk": "the one thing in YOUR data that could make this trade fail"}}
"""

LEAD_PROMPT = """You are the {name} on a professional {instrument} trading desk. You lead the {desk}.
Your members and their jobs:
{members}

YOUR JOB:
1. Check every member's evidence against the data below. Name any member whose claim is wrong or not supported.
2. Weigh the members: one well-evidenced SKIP outweighs several weak TAKEs.
3. Give ONE verdict for your desk. It is sent to the three verifiers and to the head of the desk.

Setup:
{setup}

Time / session: {session}

Data your desk used:
{data}

Your members' reports:
{reports}

Reply with JSON only:
{{"vote": "TAKE" or "SKIP", "score": 0-100, "summary": "one sentence: the desk's finding",
  "points": ["up to 3 key findings with exact numbers"],
  "doubtful": ["keys of members whose claims are wrong or weak, e.g. 'volume'"]}}
"""

DEBATE_PROMPT = """You are the {name} on a professional {instrument} trading desk. Your only job:
{focus}

Setup:
{setup}

YOUR DATA:
{data}

Your first report was: {own}

Your desk lead challenges you:
{challenges}

Re-check YOUR data only. Change your vote if the challenge is right, keep it if the data supports you.
Reply with JSON only:
{{"vote": "TAKE" or "SKIP", "score": 0-100, "changed": true or false,
  "reply": "one sentence answering the challenge with an exact number", "summary": "your updated one-sentence view"}}
"""

VERIFIER_PROMPT = """You are the {name} on a professional {instrument} trading desk. Your job:
{focus}

The engine proposed this setup:
{setup}

Market read on the setup's timeframes:
{market}

Time / session and upcoming news: {session}

Strategy board ({board_summary}):
{board}

The three desk leads report (after checking their analysts):
{desks}

All 18 analysts (one line each):
{reports}

Check the desks' claims against the data above and call out anything unsupported. Judge only from your own role.
Be strict. Reply with JSON only:
{{"vote": "TAKE" or "SKIP", "score": 0-100, "summary": "one short sentence", "points": ["up to 3 short points"]}}
"""

HEAD_PROMPT = """You are the Head Trader of a professional {instrument} desk.
The engine proposed this setup:
{setup}

Market read on the setup's timeframes:
{market}

Time / session and upcoming news: {session}

Strategy board: {board_summary}

Analyst tally: {tally}

Desk verdicts (each desk lead checked its analysts):
{desks}

Verifiers:
{verifiers}

Desk track record for this trading style (learn from it): {history}

Each agent's own record on past signals (trust the reliable ones more): {records}

Make the final call. Only TAKE a trade when technicals, strategies and macro line up and the risk is clean;
SKIP otherwise - a missed trade costs nothing, a bad one costs money.
You may fine-tune entry / stop loss / targets (keep the same direction; TP1 must be at least 1:{min_rr}),
or keep the engine's levels. confidence is 0-100 and must be honest.
Reply with JSON only:
{{"decision": "TAKE" or "SKIP", "confidence": 0-100, "entry": number, "stop_loss": number,
  "tp1": number, "tp2": number, "tp3": number,
  "headline": "max 8 words, e.g. 'Sweep + H1 OB retest in discount'",
  "reason": "1-2 short sentences in simple English"}}
"""

AUDITOR_PROMPT = """You are the Signal Auditor, the last check before a {instrument} signal is sent to traders.
Setup from the engine:
{setup}

Final decision from the head of the desk: {decision}
Final levels: {levels}

Desk verdicts and verifiers:
{reports}

Check that the signal is consistent: direction matches the reasons, the stop is beyond the invalidation level,
TP1 is realistic (not behind an opposing zone), R:R at least 1:{min_rr}, and nothing in the reports is a clear
reason to stop it. Veto only for a concrete problem.
Reply with JSON only:
{{"approve": true or false, "issues": ["up to 3 short issues"], "note": "one short sentence"}}
"""

MARKET_VIEW_PROMPT = """You are a senior {instrument} analyst who trades Smart Money Concepts.
Market read (per timeframe):
{market}

Session: {session}

Write a short market outlook for traders on Telegram (max 90 words, simple English, no markdown symbols):
overall bias per timeframe, key liquidity above and below, the nearest order blocks / FVGs to watch,
and what would make you buy or sell. Do not invent prices that are not in the data.
"""


def _r(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) and not isinstance(x, bool) else x


def _indicators(ind: dict, keep=None) -> dict:
    out = {}
    for k, v in ind.items():
        if keep is not None and k not in keep:
            continue
        if v is None:
            continue
        out[k] = {kk: _r(vv) for kk, vv in v.items()} if isinstance(v, dict) else _r(v)
    return out


# Kept for callers that still ask for one analyst's per-timeframe slice by key.
AGENT_FIELDS = {a["key"]: a["fields"] + (("indicators",) if a.get("ind") else ()) for a in ANALYSTS}
TF_ORDER = ("1day", "4h", "1h", "15min", "5min")


def _tf_slice(m: dict, fields, ind_keep=None) -> dict:
    s, v = m["smc"], m["volume"]
    full = {
        "price": _r(m["price"]),
        "atr": _r(s["atr"]),
        "trend": s["trend"],
        "structure_events": [f"{e['type']} {e['direction']} @ {e['level']:.2f}" for e in s["events"][-3:]],
        "order_blocks": [f"{z['direction']} {z['bottom']:.2f}-{z['top']:.2f}{' (tested)' if z['touched'] else ''}"
                         for z in s["order_blocks"][-3:]],
        "fvgs": [f"{z['direction']} {z['bottom']:.2f}-{z['top']:.2f}" for z in s["fvgs"][-3:]],
        "breaker_blocks": [f"{z['direction']} {z['bottom']:.2f}-{z['top']:.2f}" for z in s.get("breakers", [])[-3:]],
        "buy_side_liquidity": s["liquidity"]["buy_side"][:3],
        "sell_side_liquidity": s["liquidity"]["sell_side"][:3],
        "equal_highs": s["liquidity"]["equal_highs"][-2:],
        "equal_lows": s["liquidity"]["equal_lows"][-2:],
        "recent_sweeps": [f"{w['side']} swept @ {w['level']:.2f}" for w in s["liquidity"]["sweeps"][-2:]],
        "range": s["range"],
        "regime": m.get("regime"),
        "fib": {k: _r(x) for k, x in (m.get("fib") or {}).items()},
        "candle_patterns": [f"{x['name']}" + (f" @ {x['level']}" if "level" in x else f" ({x['age']} candles ago)")
                            for x in s.get("patterns", [])][-4:],
        "recent_candles": s.get("recent_candles", [])[-6:],
        "volume": v if not v.get("available") else {k: v.get(k) for k in (
            "relative_volume_last_closed", "delta", "pressure", "poc", "value_area_high", "value_area_low", "bubbles")},
    }
    out = {k: full[k] for k in fields if k in full}
    if ind_keep is not None or "indicators" in fields:
        out["indicators"] = _indicators(m["ind"], ind_keep)
    return out


def market_brief(market: dict, agent: str | None = None, tfs=None) -> str:
    """Compact JSON of the per-timeframe read. With `agent`, only that analyst's fields; with `tfs`, only
    those timeframes (verifiers and the head trader get the setup's three timeframes)."""
    spec = next((a for a in ANALYSTS if a["key"] == agent), None)
    if spec:
        return agent_data(spec, {"_market": market})
    out = {"key_levels": market.get("levels", {}), "average_daily_range": market.get("adr", {}),
           "pivots": market.get("pivots", {})}
    all_fields = ("price", "atr", "trend", "regime", "structure_events", "order_blocks", "fvgs", "breaker_blocks",
                  "buy_side_liquidity", "sell_side_liquidity", "equal_highs", "equal_lows", "recent_sweeps", "range",
                  "candle_patterns", "recent_candles", "volume", "indicators")
    for tf in TF_ORDER:
        if tf in market and (not tfs or tf in tfs):
            out[TF_LABEL[tf]] = _tf_slice(market[tf], all_fields)
    return json.dumps(out, separators=(",", ":"))


def _analyst_market(market: dict, spec: dict) -> dict:
    out = {}
    if spec["fields"]:
        for tf in TF_ORDER:
            if tf in market:
                out[TF_LABEL[tf]] = _tf_slice(market[tf], spec["fields"], spec.get("ind"))
    return out


def agent_data(spec: dict, ctx: dict) -> str:
    """Everything one analyst receives - its own slice of the market plus its own extra inputs."""
    market, extra = ctx["_market"], ctx.get("_extra", {})
    out = _analyst_market(market, spec)
    for x in spec.get("extras", ()):
        if x == "key_levels":
            out["key_levels"] = market.get("levels", {})
        elif x == "adr":
            out["average_daily_range"] = market.get("adr", {})
        elif x == "pivots":
            out["floor_pivots"] = market.get("pivots", {})
        elif x.startswith("strategies:"):
            groups = tuple(x.split(":", 1)[1].split(","))
            board = ctx.get("_board") or {}
            out["strategy_board"] = strategies.brief(board, groups)
        elif x == "calendar":
            out["economic_calendar_next_hours"] = extra.get("calendar") or ["no high/medium USD events"]
            out["trade_lifetime_minutes"] = ctx.get("_expiry")
        elif x.startswith("headlines:"):
            cat = x.split(":", 1)[1]
            if cat == "instrument":
                cat = "crypto" if "BTC" in ctx.get("instrument", "") else "gold"
            items = (extra.get("headlines") or {}).get(cat)
            out[f"{cat}_headlines_last_24h"] = items or ["no relevant headlines in the last 24h"]
        elif x == "intermarket":
            out["other_markets"] = extra.get("intermarket") or "no other market data"
    return json.dumps(out, separators=(",", ":"))


def agent_records(closed_trades: list[dict]) -> dict[str, dict]:
    """Each agent's record on finished signals: it was right when it said TAKE and the trade reached TP1,
    or said SKIP and the trade lost. Smoothed accuracy (starts at 50 %) becomes its voting weight."""
    rec: dict[str, dict] = {}
    for t in closed_trades:
        if t.get("outcome") not in ("win", "loss", "breakeven") or not t.get("reports"):
            continue
        won = (t.get("stage") or 0) >= 1
        for r in t["reports"]:
            key = r.get("key")
            if not key or r.get("vote") not in ("TAKE", "SKIP"):
                continue
            x = rec.setdefault(key, {"n": 0, "correct": 0, "take": 0, "take_wins": 0})
            x["n"] += 1
            x["correct"] += (r["vote"] == "TAKE") == won
            if r["vote"] == "TAKE":
                x["take"] += 1
                x["take_wins"] += won
    for x in rec.values():
        x["accuracy"] = round(100 * x["correct"] / x["n"])
        x["weight"] = round(0.5 + (x["correct"] + 2) / (x["n"] + 4), 3)  # 1.0 with no history
    return rec


def records_text(records: dict, agents: list[dict], min_n: int = 3) -> str:
    rows = sorted(((a["name"], records[a["key"]]) for a in agents if records.get(a["key"], {}).get("n", 0) >= min_n),
                  key=lambda x: -x[1]["accuracy"])
    if not rows:
        return "not enough finished signals yet"
    return "; ".join(f"{name} {r['accuracy']}% right ({r['n']} signals)" for name, r in rows)


def setup_brief(setup: dict) -> str:
    keep = ("style_label", "direction", "entry_type", "entry", "stop_loss", "tps", "price", "atr",
            "score", "confluences", "poi", "timeframes", "expiry_min")
    return json.dumps({k: setup[k] for k in keep if k in setup}, separators=(",", ":"))


def _reports_text(reports: list[dict]) -> str:
    return "\n".join(f"- {r['name']}: {r['vote']} ({r['score']}) - {r['summary']} | evidence: {r['points']}"
                     for r in reports)


def _one_liners(reports: list[dict]) -> str:
    return "\n".join(f"- [{r.get('desk', '')}] {r['name']}: {r['vote']} {r['score']} - {r['summary']}" for r in reports)


def parse_json(text: str) -> dict:
    """Parse the model's JSON, tolerating ```json fences around it."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError(f"AI did not return JSON: {(text or '')[:200]}")
    return json.loads(match.group(0))


def seconds_until_quota_reset(now: datetime | None = None) -> float:
    """Gemini free quotas reset at midnight Pacific time (~07:00 UTC in summer, 08:00 in winter)."""
    now = now or datetime.now(timezone.utc)
    reset = now.replace(hour=7 if 3 <= now.month <= 10 else 8, minute=5, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return (reset - now).total_seconds()


def classify_error(error: Exception) -> tuple[str, str, float]:
    """(kind, friendly text, seconds to rest the model) for a Gemini error."""
    t = str(error)
    low = t.lower()
    if "429" in t or "resource_exhausted" in low or "quota" in low:
        if "perday" in low or "per_day" in low or "per day" in low or "daily" in low or "month" in low:
            wait = seconds_until_quota_reset()
            return "quota_day", f"Free daily quota used up – resets in ~{int(wait // 3600)}h {int(wait % 3600 // 60)}m", wait
        m = re.search(r"retry in ([\d.]+)s", t)
        wait = float(m.group(1)) + 1 if m else 60.0
        return "quota_min", f"Per-minute limit hit – free again in {int(wait)}s", wait
    if "503" in t or "unavailable" in low or "overloaded" in low or "high demand" in low:
        return "busy", "Google model overloaded right now – switched to another model", 30.0
    if "404" in t or "not_found" in low or "no longer available" in low:
        return "missing", "Model not available for this key", 86400.0
    if "api key" in low or "api_key" in low or "401" in t or "403" in t or "permission" in low:
        return "key", "API key rejected – check the key in .env", 3600.0
    if "all ai models are resting" in low:
        return "exhausted", "All AI models are resting (daily quota used or busy)", 0.0
    return "other", t[:140], 30.0


class ModelPool:
    """Spreads calls over Gemini "slots" (model × API key) and learns which ones work.

    * the free tier limits requests per minute per model, so calls are rate-limited per slot
    * a failing slot rests: quota errors until the quota resets, overloads with growing back-off
    * healthy slots (high success rate, no recent failures) are tried first
    """

    def __init__(self, models: list[str], rpm: int = 4):
        self.models = models
        self.rpm = rpm
        self.rpm_map: dict[str, int] = {}
        self.calls = {m: deque() for m in models}
        self.cool_until = {m: 0.0 for m in models}
        self.last_error: dict[str, dict] = {}
        self.ok = {m: 0 for m in models}
        self.fail = {m: 0 for m in models}
        self.streak = {m: 0 for m in models}
        self.latency: dict[str, float] = {}

    def _ready_at(self, m: str, now: float) -> float:
        q = self.calls[m]
        while q and now - q[0] > 60:
            q.popleft()
        slot = q[0] + 60 if len(q) >= self.rpm_map.get(m, self.rpm) else now
        return max(slot, self.cool_until[m])

    def health(self, m: str) -> float:
        return (self.ok[m] + 1) / (self.ok[m] + self.fail[m] + 2) - 0.15 * self.streak[m]

    def order(self, preferred) -> list[str]:
        pref = [m for m in ([preferred] if isinstance(preferred, str) else preferred or []) if m in self.calls]
        rest = sorted((m for m in self.models if m not in pref), key=self.health, reverse=True)
        return pref + rest

    async def acquire(self, preferred, tried: set) -> str | None:
        """Next usable slot. Waits for a rate-limit slot, and for a resting slot that is free again within
        `max_wait` seconds (per-minute limits, short "busy" back-offs) - but never for a daily-quota slot."""
        order = self.order(preferred)
        max_wait = getattr(self, "max_wait", 75.0)
        deadline = time.monotonic() + max_wait
        while True:
            now = time.monotonic()
            untried = [m for m in order if m not in tried]
            left = [m for m in untried if self.cool_until[m] <= now]
            if not left:
                soon = [self.cool_until[m] for m in untried if self.cool_until[m] <= deadline]
                if not soon:
                    return None
                await asyncio.sleep(min(max(min(soon) - now, 0.5), 65))
                continue
            for m in left:
                if self._ready_at(m, now) <= now:
                    self.calls[m].append(now)
                    self.today(m)
                    return m
            await asyncio.sleep(min(max(min(self._ready_at(m, now) for m in left) - now, 0.5), 65))

    def today(self, m: str | None = None) -> dict:
        """Requests sent today per slot (resets at midnight UTC), to see how much quota the desk uses."""
        day = datetime.now(timezone.utc).date().isoformat()
        if getattr(self, "_day", None) != day:
            self._day, self.sent_today = day, {}
        if m:
            self.sent_today[m] = self.sent_today.get(m, 0) + 1
        return self.sent_today

    def resting_all(self, within: float = 75.0) -> bool:
        """True when no slot at all can be used within `within` seconds (e.g. every daily quota used)."""
        now = time.monotonic()
        return all(self.cool_until[m] > now + within for m in self.models)

    def success(self, m: str, seconds: float):
        self.ok[m] += 1
        self.streak[m] = 0
        prev = self.latency.get(m)
        self.latency[m] = seconds if prev is None else 0.7 * prev + 0.3 * seconds

    def penalize(self, m: str, error: Exception):
        kind, friendly, wait = classify_error(error)
        self.fail[m] += 1
        self.streak[m] += 1
        if kind in ("busy", "other"):  # back off harder each time a slot keeps failing
            wait = min(wait * 2 ** (self.streak[m] - 1), 900)
        self.cool_until[m] = time.monotonic() + wait
        self.last_error[m] = {"kind": kind, "text": friendly, "until": time.time() + wait}

    def export(self) -> dict:
        """Resting slots in wall-clock time, so a restart does not re-discover dead quotas."""
        now = time.time()
        return {m: e for m, e in self.last_error.items() if e["until"] > now and e["kind"] in ("quota_day", "missing")}

    def restore(self, saved: dict | None):
        now_w, now_m = time.time(), time.monotonic()
        for m, e in (saved or {}).items():
            if m in self.cool_until and e.get("until", 0) > now_w:
                self.cool_until[m] = now_m + (e["until"] - now_w)
                self.last_error[m] = e

    def status(self) -> list[dict]:
        """Per-slot state for the dashboard."""
        now_m, now = time.monotonic(), time.time()
        out = []
        for m in self.models:
            err = self.last_error.get(m)
            cooling = self.cool_until[m] > now_m
            model, k = split_slot(m)
            reason = err["text"] if err and cooling else ""
            first = f"{model}#1"
            if (cooling and err and err["kind"] == "quota_day" and k > 0 and self.ok[m] == 0
                    and self.ok.get(first, 0) > 0):
                # Never answered, yet "daily quota used": the quota is shared with key 1.
                reason += (" – this key hit the daily limit without being used: it is probably in the SAME Google "
                           "project/account as key 1 (keys of one project share one quota). Make it in another "
                           "Google account.")
            out.append({"model": slot_label(m), "name": model, "key": k + 1, "lite": is_lite(model),
                        "ready": not cooling,
                        "kind": err["kind"] if err and cooling else None,
                        "reason": reason,
                        "back_in_min": int(max(err["until"] - now, 0) // 60) if err and cooling else 0,
                        "calls_last_min": len(self.calls[m]), "today": self.today().get(m, 0),
                        "ok": self.ok[m], "fail": self.fail[m],
                        "latency": round(self.latency[m], 1) if m in self.latency else None})
        return out


def is_lite(model: str) -> bool:
    """Small / fast models, used for the narrow analyst jobs."""
    m = model.lower()
    return any(x in m for x in ("lite", "small", "mini", "-8b", "20b", "flash-8b"))


def split_slot(slot: str) -> tuple[str, int]:
    """'gemini-3.6-flash#2' -> ('gemini-3.6-flash', 1): model name and API-key index."""
    model, _, k = slot.partition("#")
    return model, (int(k) - 1 if k else 0)


def slot_label(slot: str) -> str:
    model, k = split_slot(slot)
    return f"{model} · key {k + 1}" if "#" in slot else model


class TradingDesk:
    def __init__(self, api_key: str | list[str], model: str, min_rr: float, min_confidence: int, min_votes: int,
                 fallback_models: list[str] | None = None, rpm_per_model: int = 4, providers: list[dict] | None = None):
        keys = [api_key] if isinstance(api_key, str) else [k for k in api_key if k]
        self.clients = [genai.Client(api_key=k) for k in keys]
        self.client = self.clients[0] if self.clients else None
        self.key_info = [{"name": "Gemini", "kind": "gemini", "n": i + 1} for i in range(len(keys))]
        # Other free APIs (Mistral, Groq – OpenAI-compatible): each key is one more "key" in the pool.
        extra = []
        for p in providers or []:
            self.clients.append(None)
            n = sum(1 for x in self.key_info if x["name"] == p["name"]) + 1
            self.key_info.append({"name": p["name"], "kind": "openai", "n": n, "base": p["base"], "key": p["key"],
                                  "interval": p.get("interval", 1.1)})
            extra.append((len(self.clients) - 1, list(p["models"])))
        multi = len(self.clients) > 1
        models = [model] + [m for m in (fallback_models or []) if m != model]
        # Every model on every key is its own "slot" with its own free quota. Neighbouring agents get
        # different keys, so one key's limits never stop the whole desk.
        slots = [f"{m}#{k + 1}" for m in models for k in range(len(keys))] if multi else models
        slots += [f"{m}#{k + 1}" for k, ms in extra for m in ms]
        self.pool = ModelPool(slots, rpm_per_model)
        for k, ms in extra:  # these providers limit per account (spaced in _generate), not per model
            for m in ms:
                self.pool.rpm_map[f"{m}#{k + 1}"] = 40
        self.base_models = models
        self.plan = self._make_plan(models, len(keys), extra, multi)
        self.min_rr = min_rr
        self.min_confidence = min_confidence
        self.min_votes = min_votes
        self.usage = {"day": None, "calls": 0, "failures": 0}
        self.monitor = Monitor()

    def _count(self, failed: bool):
        from datetime import date
        if self.usage["day"] != date.today():
            self.usage.update(day=date.today(), calls=0, failures=0)
        self.usage["calls"] += 1
        self.usage["failures"] += failed

    @staticmethod
    def _make_plan(models: list[str], n_keys: int, extra=(), multi: bool | None = None) -> dict[str, list[str]]:
        """With other providers (Mistral/Groq) configured they go first – they have far bigger free quotas –
        and the Gemini plan below becomes the fallback."""
        base = TradingDesk._gemini_plan(models, n_keys, n_keys > 1 if multi is None else multi)
        ex = [f"{m}#{k + 1}" for k, ms in extra for m in ms]
        if not ex:
            return base
        lite = [x for x in ex if is_lite(split_slot(x)[0])]
        strong = [x for x in ex if x not in lite]
        fast = lite + strong[1:] or strong  # analysts: small + mid models; decisions: the biggest first
        plan = {}
        for i, a in enumerate(ANALYSTS):
            n = i % max(len(fast), 1)
            plan[a["key"]] = fast[n:] + fast[:n] + [x for x in strong if x not in fast] + base[a["key"]]
        for i, a in enumerate(LEADS + VERIFIERS + [HEAD, AUDITOR]):
            order = strong or lite
            n = i % max(len(order), 1) if a in VERIFIERS else 0
            plan[a["key"]] = order[n:] + order[:n] + [x for x in lite if x not in order] + base[a["key"]]
        return plan

    @staticmethod
    def _gemini_plan(models: list[str], n_keys: int, multi: bool) -> dict[str, list[str]]:
        """Which model × key each agent tries first.

        Every agent has a home key. With 2 keys the 26 agents split 13 / 13:
          analysts   alternate key 1 / key 2 (9 + 9)
          leads      technical → key 1, strategy → key 2, macro → key 1
          verifiers  confluence → key 2, risk → key 1, devil → key 2
          head       key 1            auditor  key 2 (an independent second opinion)
        The 18 analysts have narrow, data-bound jobs → fast "lite" models (much bigger free quota).
        Leads, verifiers, Head Trader and Auditor judge everything → the strongest models.
        After its own list an agent can still fall back to any healthy slot on any key.
        """
        n_keys = max(n_keys, 1)

        def slot(m: str, k: int) -> str:
            return f"{m}#{k + 1}" if multi else m

        def rot(lst: list[str], n: int) -> list[str]:
            n %= max(len(lst), 1)
            return lst[n:] + lst[:n]
        lite = [m for m in models if "lite" in m] or models
        strong = [m for m in models if "lite" not in m] or models
        plan = {}
        for i, a in enumerate(ANALYSTS):
            k = i % n_keys
            plan[a["key"]] = [slot(m, k) for m in rot(lite, i // n_keys)] + [slot(m, k) for m in strong]
        for i, lead in enumerate(LEADS):
            k = i % n_keys
            plan[lead["key"]] = [slot(m, k) for m in rot(strong, i)] + [slot(m, k) for m in lite]
        for i, v in enumerate(VERIFIERS):
            k = (i + 1) % n_keys
            plan[v["key"]] = [slot(m, k) for m in rot(strong, 1 + i)] + [slot(m, k) for m in lite]
        plan["head"] = [slot(m, 0) for m in strong] + [slot(m, n_keys - 1) for m in strong]
        plan["auditor"] = [slot(m, (1 % n_keys)) for m in rot(strong, 1)] + [slot(m, 0) for m in strong]
        return plan

    def home_key(self, key: str) -> int:
        slots = self.slots_for(key)
        return split_slot(slots[0])[1] + 1 if slots and "#" in slots[0] else 1

    def provider(self, k: int) -> dict:
        info = getattr(self, "key_info", None) or []
        return info[k] if k < len(info) else {"name": "Gemini", "kind": "gemini", "n": k + 1}

    def key_names(self) -> list[str]:
        return [f"{x['name']} {x['n']}" for x in getattr(self, "key_info", None) or []] or ["Gemini 1"]

    def label(self, slot: str) -> str:
        model, k = split_slot(slot)
        if "#" not in slot:
            return model
        p = self.provider(k)
        return f"{model} · {p['name']} key {p['n']}"

    async def _generate(self, slot: str, prompt: str, json_mode: bool = True, config=None,
                        temperature: float = 0.2) -> str:
        """One request to whichever provider owns this slot."""
        name, k = split_slot(slot)
        p = self.provider(k)
        if p["kind"] == "openai":
            await self._space(k, p.get("interval", 1.1))
            body = {"model": name, "temperature": temperature, "messages": [{"role": "user", "content": prompt}]}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            async with httpx.AsyncClient(timeout=120) as http:
                r = await http.post(p["base"].rstrip("/") + "/chat/completions", json=body,
                                    headers={"Authorization": f"Bearer {p['key']}"})
            if r.status_code != 200:
                text = r.text[:300]
                if r.status_code == 429 and "month" not in text.lower():
                    text += f" (retry in {r.headers.get('retry-after') or 5}s)"
                raise RuntimeError(f"{r.status_code} {p['name']}: {text}")
            content = r.json()["choices"][0]["message"].get("content") or ""
            return content if isinstance(content, str) else "".join(c.get("text", "") for c in content)
        config = config or types.GenerateContentConfig(
            temperature=temperature, response_mime_type="application/json" if json_mode else "text/plain")
        clients = getattr(self, "clients", None)
        client = clients[k] if clients and k < len(clients) and clients[k] else self.client
        resp = await client.aio.models.generate_content(model=name, contents=prompt, config=config)
        return resp.text or ""

    async def _space(self, k: int, interval: float):
        """Accounts limited to ~1 request/second (Mistral free): keep requests on one key spaced out."""
        locks = self.__dict__.setdefault("_locks", {})
        nxt = self.__dict__.setdefault("_next_at", {})
        lock = locks.setdefault(k, asyncio.Lock())
        async with lock:
            wait = nxt.get(k, 0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            nxt[k] = time.monotonic() + interval

    def slots_for(self, key: str) -> list[str]:
        plan = getattr(self, "plan", None) or {}
        return plan.get(key) or self.pool.models

    def model_for(self, i: int) -> str:
        models = self.pool.models
        return models[i % len(models)]

    async def _ask_json(self, prompt: str, preferred) -> tuple[dict, str]:
        """Ask for JSON; if a model answers with something unparsable, ask the next slot once."""
        text, model = await self._ask(prompt, preferred)
        try:
            return parse_json(text), model
        except (ValueError, json.JSONDecodeError):
            log.warning("Unparsable JSON from %s, asking another model", model)
            order = self.pool.order(preferred)
            text, model = await self._ask(prompt, [m for m in order if slot_label(m) != model])
            return parse_json(text), model

    async def _ask(self, prompt: str, preferred=None, json_mode: bool = True) -> tuple[str, str]:
        """Returns (text, model used)."""
        config = types.GenerateContentConfig(
            temperature=0.2, response_mime_type="application/json" if json_mode else "text/plain")
        tried, last_error = set(), None
        while True:
            model = await self.pool.acquire(preferred or self.pool.models[0], tried)
            if model is None:
                raise last_error if tried else RuntimeError("all AI models are resting (daily quota used or busy)")
            try:
                started = time.monotonic()
                text = await self._generate(model, prompt, json_mode, config)
                self._count(False)
                self.pool.success(model, time.monotonic() - started)
                return text, self.label(model)
            except Exception as e:
                self._count(True)
                last_error = e
                tried.add(model)
                self.pool.penalize(model, e)
                log.warning("AI %s failed: %s", self.label(model), str(e)[:120])


    def agent_models(self) -> dict[str, str]:
        """Which model each agent tries first."""
        keys = [a["key"] for a in ALL_AGENTS]
        return {k: self.slots_for(k)[0] for k in keys}

    async def ping(self) -> list[dict]:
        """Health check that spends as little free quota as possible: one tiny request per MODEL
        (not per agent), then every agent is marked by the model it uses."""
        mon = self.monitor
        now = time.time()
        if now - getattr(self, "_last_ping", 0) < 90:
            mon.event("Agent test was run less than 90 s ago – please wait (it uses your free Gemini quota)", "skip")
            return []
        self._last_ping = now
        mapping = self.agent_models()
        mon.event(f"Testing the Gemini models/keys used by the {len(mapping)} agents…")
        for key in mapping:
            mon.agent(key, "thinking", summary="Health check…", vote=None, score=None, points=[], err=None)

        async def one(model: str) -> dict:
            started = time.monotonic()
            if self.pool.cool_until.get(model, 0) > time.monotonic():
                err = self.pool.last_error.get(model, {})
                return {"model": model, "ok": False, "kind": err.get("kind"), "error": err.get("text", "resting")}
            try:
                text = await self._generate(model, 'Reply with exactly this JSON object: {"ok": true}', True,
                                            temperature=0)
                self._count(False)
                ok = bool(parse_json(text).get("ok"))
                return {"model": model, "ok": ok, "seconds": round(time.monotonic() - started, 1)}
            except Exception as e:
                self._count(True)
                self.pool.penalize(model, e)
                kind, friendly, _ = classify_error(e)
                return {"model": model, "ok": False, "kind": kind, "error": friendly}

        slots = list(dict.fromkeys(mapping.values())) + [m for m in self.pool.models if m not in mapping.values()][:3]
        results = {r["model"]: r for r in await asyncio.gather(*(one(m) for m in slots))}
        online = [m for m, r in results.items() if r["ok"]]
        for key, model in mapping.items():
            r = results.get(model, {})
            if r.get("ok"):
                mon.agent(key, "done", vote=None, model=slot_label(model), seconds=r.get("seconds"), err=None,
                          summary=f"Online ({slot_label(model)} answered in {r.get('seconds')}s)")
            elif online:
                mon.agent(key, "done", vote=None, model=slot_label(online[0]), err=None,
                          summary=f"Online via backup {slot_label(online[0])} ({slot_label(model)}: {r.get('error')})")
            else:
                mon.agent(key, "error", err=r.get("kind"), summary=r.get("error") or "no model available")
        ok_agents = len(mapping) if online else 0
        mon.event(f"Models online: {len(online)}/{len(results)} → agents working: {ok_agents}/{len(mapping)}",
                  "take" if online else "error")
        if not online:
            kinds = {r.get("kind") for r in results.values()}
            if "quota_day" in kinds:
                mon.event("All Gemini models used their free daily quota. The AI desk resumes after the reset "
                          "(midnight Pacific ≈ 07:00 UTC). Engine-only signals (score ≥ ENGINE_ONLY_SCORE) still work.",
                          "error")
        return list(results.values())


    async def _call(self, agent: dict, prompt: str, mon: Monitor, stage: int, sources: tuple = ()) -> dict:
        """Run one agent on its prompt; returns its report (vote ERROR if Gemini could not answer)."""
        key = agent["key"]
        mon.agent(key, "thinking", vote=None, score=None, summary="Working on its job…", points=[], err=None)
        started = time.monotonic()
        base = {"key": key, "name": agent["name"], "icon": agent["icon"], "stage": stage,
                "desk": agent.get("desk", "")}
        try:
            for src in sources:
                if isinstance(src, tuple):
                    mon.message(src[0], key, src[1], src[2] if len(src) > 2 else "report")
                else:
                    mon.message(src, key)
            data, model = await self._ask_json(prompt, self.slots_for(key))
            vote = str(data.get("vote", "SKIP")).upper()
            try:
                score = max(0, min(int(float(data.get("score") or 0)), 100))
            except (TypeError, ValueError):
                score = 0
            report = dict(base, model=model, vote="TAKE" if vote == "TAKE" else "SKIP", score=score,
                          summary=str(data.get("summary", ""))[:220],
                          points=[str(p)[:160] for p in (data.get("evidence") or data.get("points") or [])][:3],
                          risk=str(data.get("risk", ""))[:160],
                          doubtful=[str(x) for x in (data.get("doubtful") or [])][:9])
            pts = report["points"] + ([f"Risk: {report['risk']}"] if report["risk"] else [])
            mon.agent(key, "done", vote=report["vote"], score=score, summary=report["summary"], points=pts,
                      model=model, seconds=round(time.monotonic() - started, 1))
            mon.event(f"{agent['icon']} {agent['name']}: {report['vote']} ({score}) – {report['summary']}",
                      "take" if report["vote"] == "TAKE" else "skip")
            return report
        except Exception as e:
            log.warning("%s failed: %s", agent["name"], e)
            kind, friendly, _ = classify_error(e)
            mon.agent(key, "error", summary=friendly, err=kind, seconds=round(time.monotonic() - started, 1))
            mon.event(f"{agent['icon']} {agent['name']} failed: {friendly}", "error")
            return dict(base, vote="ERROR", score=0, summary="unavailable", points=[], risk="", doubtful=[])

    # Kept for older callers: one specialist on the old-style prompt.
    async def _specialist(self, i: int, agent: dict, ctx: dict, template: str | None = None, sources: tuple = ()):
        return await self._analyst(agent, ctx, self.monitor)

    async def _analyst(self, spec: dict, ctx: dict, mon: Monitor) -> dict:
        lead = next(x for x in LEADS if x["key"] == spec["lead"])
        data = agent_data(spec, ctx)
        prompt = ANALYST_PROMPT.format(
            name=spec["name"], desk=DESKS[spec["desk"]]["name"], instrument=ctx["instrument"], focus=spec["focus"],
            direction=ctx["direction"], lead=lead["name"], setup=ctx["setup"], session=ctx["session"], data=data)
        # Saving quota: an analyst whose input has not changed gives the same answer, so reuse it for a while.
        # News analysts only depend on the news and the direction; the others on their full prompt.
        basis = (ctx["instrument"], ctx["direction"], data) if spec["desk"] == "macro" else (prompt,)
        ck = (spec["key"], hashlib.sha1(repr(basis).encode()).hexdigest())
        ttl = 45 * 60 if spec["desk"] == "macro" else 20 * 60
        cache = self.__dict__.setdefault("_cache", {})
        hit = cache.get(ck)
        if hit and time.time() - hit[0] < ttl:
            r = dict(hit[1], summary=hit[1]["summary"], reused=True)
            mon.message("engine", spec["key"], ctx["_brief"])
            mon.agent(spec["key"], "done", vote=r["vote"], score=r["score"], summary=r["summary"],
                      points=r["points"] + [f"Reused: input unchanged since {int((time.time() - hit[0]) // 60)} min ago"],
                      model=r.get("model"), seconds=0, err=None)
            mon.message(spec["key"], spec["lead"], f"{r['vote']} {r['score']} – {r['summary']}")
            return r
        r = await self._call(spec, prompt, mon, 1, sources=(("engine", ctx["_brief"]),))
        if r["vote"] != "ERROR":
            cache[ck] = (time.time(), dict(r))
            for k in [k for k, v in cache.items() if time.time() - v[0] > 3600]:
                cache.pop(k, None)
        if r["vote"] != "ERROR":  # hand the finding straight to the desk lead
            mon.message(spec["key"], spec["lead"], f"{r['vote']} {r['score']} – {r['summary']}")
        return r

    @staticmethod
    def _desk_data(desk: str, ctx: dict) -> str:
        market, extra = ctx["_market"], ctx.get("_extra", {})
        if desk == "tech":
            return ctx["market"]
        if desk == "strategy":
            return json.dumps({"strategy_board": strategies.brief(ctx.get("_board") or {}),
                               "key_levels": market.get("levels", {}), "floor_pivots": market.get("pivots", {}),
                               "fib_and_range": {TF_LABEL[tf]: {"range": market[tf]["smc"]["range"],
                                                                "fib": {k: _r(v) for k, v in market[tf].get("fib", {}).items()}}
                                                 for tf in ctx["_tfs"] if tf in market},
                               "trend_by_timeframe": {TF_LABEL[tf]: market[tf]["smc"]["trend"]
                                                      for tf in TF_ORDER if tf in market}}, separators=(",", ":"))
        return json.dumps({"economic_calendar": extra.get("calendar") or ["no high/medium USD events"],
                           "trade_lifetime_minutes": ctx.get("_expiry"),
                           "headlines": extra.get("headlines") or {}, "other_markets": extra.get("intermarket")},
                          indent=1)

    async def _lead(self, lead: dict, members: list[dict], ctx: dict, mon: Monitor) -> dict:
        specs = {a["key"]: a for a in ANALYSTS}
        roster = "\n".join(f"- {m['key']}: {m['name']} – {specs[m['key']]['focus'][:110]}" for m in members)
        answered = [m for m in members if m["vote"] != "ERROR"]
        if not answered:
            mon.agent(lead["key"], "error", summary="none of the desk's analysts answered", err="other")
            return {"key": lead["key"], "name": lead["name"], "icon": lead["icon"], "stage": 2, "desk": lead["desk"],
                    "vote": "ERROR", "score": 0, "summary": "no analyst reports", "points": [], "risk": "",
                    "doubtful": []}
        prompt = LEAD_PROMPT.format(name=lead["name"], instrument=ctx["instrument"], desk=DESKS[lead["desk"]]["name"],
                                    members=roster, setup=ctx["setup"], session=ctx["session_news"],
                                    data=self._desk_data(lead["desk"], ctx), reports=_reports_text(answered))
        return await self._call(lead, prompt, mon, 2)

    async def _debate(self, leads: list[dict], analysts: list[dict], ctx: dict, mon: Monitor):
        """Each desk lead challenges the members who disagree with the desk or whose evidence it doubts; the
        member re-checks its own data and answers (it may change its vote)."""
        specs = {a["key"]: a for a in ANALYSTS}
        jobs = []
        for lead in leads:
            if lead["vote"] == "ERROR":
                continue
            members = [r for r in analysts if r["desk"] == lead["desk"] and r["vote"] != "ERROR"]
            targets = [r for r in members if r["vote"] != lead["vote"] or r["key"] in lead.get("doubtful", [])]
            targets = sorted(targets, key=lambda r: -r["score"])[:2]  # the 2 most confident dissenters
            jobs += [(lead, r) for r in targets]
        if not jobs:
            return
        mon.event(f"Debate: desk leads challenge {len(jobs)} analysts")

        async def answer(lead: dict, r: dict):
            spec = specs[r["key"]]
            why = (f"{lead['name']} ({lead['vote']} {lead['score']}): {lead['summary']}"
                   + (" – your evidence looks wrong or weak" if r["key"] in lead.get("doubtful", []) else ""))
            mon.message(lead["key"], r["key"], f"Challenge: {lead['summary']}", "challenge")
            mon.agent(r["key"], "thinking", summary="Answering the desk lead's challenge…")
            try:
                d, model = await self._ask_json(DEBATE_PROMPT.format(
                    name=spec["name"], instrument=ctx["instrument"], focus=spec["focus"], setup=ctx["setup"],
                    data=agent_data(spec, ctx), own=f"{r['vote']} {r['score']} – {r['summary']} {r['points']}",
                    challenges=why), self.slots_for(spec["key"]))
            except Exception as e:
                kind, friendly, _ = classify_error(e)
                mon.agent(r["key"], "done", vote=r["vote"], score=r["score"], summary=r["summary"])
                mon.event(f"{spec['icon']} {spec['name']} could not answer ({friendly}); keeps {r['vote']}", "error")
                return
            old = r["vote"]
            vote = "TAKE" if str(d.get("vote", old)).upper() == "TAKE" else "SKIP"
            try:
                score = max(0, min(int(float(d.get("score") or r["score"])), 100))
            except (TypeError, ValueError):
                score = r["score"]
            r.update(vote=vote, score=score, debate=str(d.get("reply", ""))[:200],
                     summary=str(d.get("summary") or r["summary"])[:220], changed=vote != old)
            mon.agent(r["key"], "done", vote=vote, score=score, summary=r["summary"], model=model,
                      points=[f"Debate: {r['debate']}"] + r.get("points", [])[:2])
            mon.message(r["key"], lead["key"], f"{'Changed to ' + vote if r['changed'] else 'Keeps ' + vote}: "
                                               f"{r['debate']}", "reply")
            mon.event(f"Debate · {spec['name']}: "
                      + (f"changed {old} → {vote}" if r["changed"] else f"keeps {vote}") + f" – {r['debate']}",
                      "take" if vote == "TAKE" else "skip")

        await asyncio.gather(*(answer(lead, r) for lead, r in jobs))

    def _need(self, answered: int) -> int:
        """Analysts that must agree: MIN_AGREE_PCT of those that answered (default: MIN_AGENT_VOTES out of 8)."""
        pct = getattr(self, "min_agree_pct", None) or self.min_votes / 8
        return max(1, math.ceil(answered * pct - 1e-9))

    async def review(self, setup: dict, market: dict, session: dict, history: str = "",
                     practice: bool = False, instrument: str = "XAU/USD (gold)") -> dict:
        """Run the whole desk on one setup. Returns a verdict dict with 'approved'."""
        extra = {"calendar": session.get("news") or [],
                 "headlines": session.get("headlines_by_cat") or {"gold": session.get("headlines") or [],
                                                                  "macro": session.get("headlines") or []},
                 "intermarket": session.get("intermarket")}
        sess = {k: session[k] for k in ("sessions", "killzone", "utc_time") if k in session}
        tfs = [t for t in (setup.get("timeframes") or {}).values() if t in market] or list(TF_ORDER)
        sess["market_regime"] = {TF_LABEL[t]: (market[t].get("regime") or {}).get("regime") for t in tfs}
        board = strategies.evaluate(market, setup["direction"], setup.get("timeframes") or {}, setup["entry"])
        brief = (f"{setup['style_label']} {setup['direction']} {setup['entry_type']} @ {setup['entry']} · SL "
                 f"{setup['stop_loss']} · TP1 {setup['tps'][0]['price']} · engine score {setup['score']} · "
                 + "; ".join(setup["confluences"][:3]))
        ctx = {"setup": setup_brief(setup), "session": json.dumps(sess),
               "session_news": json.dumps({**sess, "upcoming_usd_news": extra["calendar"]}),
               "market": market_brief(market, tfs=tfs), "history": history, "instrument": instrument,
               "direction": setup["direction"], "_market": market, "_extra": extra, "_board": board,
               "_tfs": tfs, "_expiry": setup.get("expiry_min"), "_brief": brief}
        mon = getattr(self, "monitor", None) or Monitor()
        mon.set_phase("ai_review")
        mon.board = board
        for a in LEADS + VERIFIERS + [HEAD, AUDITOR]:
            mon.agent(a["key"], "waiting", summary="Waiting for reports…", vote=None, score=None, points=[])
        mon.event(("PRACTICE review (no signal will be sent): " if practice else "AI desk reviewing ")
                  + f"{setup.get('symbol_name', 'XAU/USD')} {setup['style_label']} {setup['direction']} @ {setup['entry']} (engine score {setup['score']})")
        mon.event(f"Strategy board ({board['regime']} market): {board['agrees']} agree · {board['against']} against · {board['neutral']} neutral")
        mon.review_start(f"{setup.get('symbol_name', 'XAU/USD')} {setup['style_label']} {setup['direction']} @ {setup['entry']}",
                         practice)
        verdict = await self._review(setup, ctx, mon)
        mon.review_end(verdict["approved"])
        mon.agent("head", "done" if not verdict.get("ai_down") else "error",
                  vote="TAKE" if verdict["approved"] else "SKIP", score=verdict["confidence"],
                  summary=verdict.get("reason") or verdict.get("reject_reason") or "")
        mon.event(f"Head Trader: {'APPROVED' if verdict['approved'] else 'REJECTED'} – "
                  f"{verdict.get('reject_reason') or verdict.get('headline') or verdict.get('reason')}",
                  "take" if verdict["approved"] else "skip")
        mon.review({"practice": practice, "symbol": setup.get("symbol_name", "XAU/USD"), "style": setup["style_label"],
                    "direction": setup["direction"], "entry": setup["entry"],
                    "score": setup["score"], "votes": verdict["votes"], "approved": verdict["approved"],
                    "confidence": verdict["confidence"], "reason": verdict.get("reject_reason") or verdict.get("reason"),
                    "board": f"{board['agrees']}/{board['agrees'] + board['against']}",
                    "reports": [{k: r.get(k) for k in ("icon", "name", "vote", "score", "changed", "stage")}
                                for r in verdict["reports"]]})
        mon.set_phase("idle")
        return verdict

    def _out_of_quota(self, verdict: dict, mon: Monitor, pending: list[dict]) -> dict | None:
        """Stop cleanly when no model slot can be used any more today, instead of sending doomed requests."""
        if not self.pool.resting_all():
            return None
        for a in pending:
            mon.agent(a["key"], "error", err="exhausted", summary="Skipped – no Gemini model available (daily quota used)")
        mon.event("AI desk stopped: every Gemini model/key is resting (daily quota used). It resumes after the reset "
                  "(midnight Pacific ≈ 07:00 UTC) – add another key from a different Google account for more.", "error")
        verdict.update(ai_down=True, reason="Gemini quota used on every key",
                       reject_reason="the AI desk ran out of Gemini quota before finishing the review")
        return verdict

    async def _review(self, setup: dict, ctx: dict, mon: Monitor) -> dict:
        board = ctx["_board"]
        early = {"reports": [], "votes": 0, "errors": 0, "approved": False, "board": board,
                 "confidence": 0, "headline": "", "reason": "", "levels": None}
        if self._out_of_quota(early, mon, ANALYSTS + LEADS + VERIFIERS + [HEAD, AUDITOR]):
            return early
        # Stage 1: the engine hands the setup to the 18 analysts; each works on its own data in parallel.
        mon.event(f"Stage 1: engine → {len(ANALYSTS)} analysts (technical, strategy, macro desks)")
        analysts = list(await asyncio.gather(*(self._analyst(a, ctx, mon) for a in ANALYSTS)))
        verdict = {"reports": list(analysts), "votes": 0, "errors": 0, "approved": False, "board": board,
                   "confidence": 0, "headline": "", "reason": "", "levels": None}
        if all(r["vote"] == "ERROR" for r in analysts):
            verdict["reason"] = "AI unavailable"
            verdict["ai_down"] = True
            return verdict

        if self._out_of_quota(verdict, mon, LEADS + VERIFIERS + [HEAD, AUDITOR]):
            return verdict

        # Stage 2: each desk lead checks its analysts, then challenges the doubtful ones (debate).
        mon.event("Stage 2: analysts → Technical / Strategy / Macro desk leads")
        leads = list(await asyncio.gather(*(self._lead(lead, [r for r in analysts if r["desk"] == lead["desk"]], ctx, mon)
                                            for lead in LEADS)))
        if getattr(self, "debate", True):
            await self._debate(leads, analysts, ctx, mon)

        if self._out_of_quota(verdict, mon, VERIFIERS + [HEAD, AUDITOR]):
            verdict["reports"] = list(analysts) + list(leads)
            return verdict

        # Stage 3: the verifiers receive the three desk verdicts (plus every analyst's one-liner).
        mon.event("Stage 3: desk verdicts → Confluence Verifier, Risk Manager, Devil's Advocate")
        board_summary = f"{board['agrees']} agree, {board['against']} against, {board['neutral']} neutral"
        vctx = dict(instrument=ctx["instrument"], setup=ctx["setup"], market=ctx["market"], session=ctx["session_news"],
                    board_summary=board_summary, board="\n".join(strategies.brief(board)),
                    desks=_reports_text(leads), reports=_one_liners(analysts))
        shared = tuple((lead["key"], f"{lead['vote']} {lead['score']} – {lead['summary']}") for lead in leads
                       if lead["vote"] != "ERROR")
        verifiers = list(await asyncio.gather(*(self._call(
            v, VERIFIER_PROMPT.format(name=v["name"], focus=v["focus"], **vctx), mon, 3, shared) for v in VERIFIERS)))

        answered = [r for r in analysts if r["vote"] != "ERROR"]
        a_votes = sum(r["vote"] == "TAKE" for r in analysts)
        l_votes = sum(r["vote"] == "TAKE" for r in leads)
        v_votes = sum(r["vote"] == "TAKE" for r in verifiers)
        need = self._need(len(answered))
        records = getattr(self, "records", None) or {}
        wsum = sum(records.get(r["key"], {}).get("weight", 1.0) for r in answered)
        weighted = round(100 * sum(records.get(r["key"], {}).get("weight", 1.0) for r in answered
                                   if r["vote"] == "TAKE") / wsum) if wsum else 0
        reports = list(analysts) + list(leads) + list(verifiers)
        per_desk = {d: f"{sum(r['vote'] == 'TAKE' for r in analysts if r['desk'] == d)}/"
                       f"{sum(1 for r in analysts if r['desk'] == d)}" for d in DESKS}
        verdict.update(reports=reports, votes=a_votes + l_votes + v_votes, analyst_votes=a_votes, lead_votes=l_votes,
                       verifier_votes=v_votes, errors=sum(r["vote"] == "ERROR" for r in reports), need=need,
                       per_desk=per_desk, weighted_agreement=weighted)
        tally = (f"{a_votes}/{len(answered)} analysts TAKE (need {need}), reliability-weighted agreement "
                 f"{weighted}% – technical {per_desk['tech']}, strategy {per_desk['strategy']}, macro {per_desk['macro']}")

        # Stage 4: the Head Trader reads the desks and the verifiers.
        for r in leads + verifiers:
            if r["vote"] != "ERROR":
                mon.message(r["key"], "head", f"{r['vote']} {r['score']} – {r['summary']}")
        mon.event("Stage 4: 3 desk verdicts + 3 verifiers → Head Trader")
        mon.agent("head", "thinking", summary="Reading the desks and verifiers and making the final call…")
        try:
            text, _ = await self._ask(HEAD_PROMPT.format(
                instrument=ctx["instrument"], setup=ctx["setup"], market=ctx["market"], session=ctx["session_news"],
                board_summary=board_summary, tally=tally, desks=_reports_text(leads),
                verifiers=_reports_text(verifiers), history=ctx.get("history") or "no closed trades yet",
                records=records_text(records, ANALYSTS + LEADS + VERIFIERS),
                min_rr=self.min_rr), preferred=self.slots_for("head"))
            head = parse_json(text)
        except Exception as e:
            # Reports did arrive, so this is not "AI down": decide on the votes alone, with a clear majority.
            log.warning("Head trader failed: %s", e)
            takers = [r["score"] for r in reports if r["vote"] == "TAKE"]
            verdict["confidence"] = round(sum(takers) / len(takers)) if takers else 0
            kind, friendly, _ = classify_error(e)
            verdict["reason"] = f"Head trader unavailable ({friendly}) – decided by the desk's votes."
            strict_need = max(need, math.ceil(len(answered) * 0.75))
            verdict["approved"] = (a_votes >= strict_need and l_votes >= 2 and v_votes >= 2
                                   and board["against"] <= board["agrees"]
                                   and verdict["confidence"] >= self.min_confidence)
            if not verdict["approved"]:
                verdict["reject_reason"] = f"head trader offline and only {a_votes}/{len(answered)} analysts agree"
            return verdict

        verdict["confidence"] = int(head.get("confidence") or 0)
        verdict["headline"] = str(head.get("headline", ""))[:80]
        verdict["reason"] = str(head.get("reason", ""))[:400]
        take = str(head.get("decision", "")).upper() == "TAKE"

        # Use the head trader's levels only if they pass the same checks as the engine's.
        try:
            levels = {"entry": float(head["entry"]), "stop_loss": float(head["stop_loss"]),
                      "tps": [float(head["tp1"]), float(head["tp2"]), float(head["tp3"])]}
            ok, why = validate_levels(setup["direction"], levels["entry"], levels["stop_loss"], levels["tps"],
                                      setup["price"], setup["atr"], self.min_rr, setup["entry_type"])
            if ok:
                verdict["levels"] = levels
            else:
                log.info("Head trader levels rejected (%s); keeping engine levels", why)
        except (KeyError, TypeError, ValueError):
            pass

        # Confirmation rules: every layer of the desk must agree.
        reasons = []
        if not take:
            reasons.append("head trader said SKIP")
        if verdict["confidence"] < self.min_confidence:
            reasons.append(f"confidence {verdict['confidence']} < {self.min_confidence}")
        if len(answered) < math.ceil(len(ANALYSTS) * 2 / 3):
            reasons.append(f"only {len(answered)}/{len(ANALYSTS)} analysts answered")
        if a_votes < need:
            reasons.append(f"only {a_votes}/{len(answered)} analysts agree (need {need})")
        elif records and weighted < 100 * need / max(len(answered), 1) - 1e-9:
            reasons.append(f"the most reliable analysts disagree (weighted agreement {weighted}%)")
        if l_votes < 2:
            reasons.append(f"only {l_votes}/{len(LEADS)} desks agree")
        if v_votes < 2:
            reasons.append(f"only {v_votes}/{len(VERIFIERS)} verifiers agree")
        if board["against"] > board["agrees"]:
            reasons.append(f"strategy board against ({board['against']} vs {board['agrees']})")
        if reasons:
            verdict["reject_reason"] = "; ".join(reasons)
            return verdict

        # Stage 5: the Signal Auditor checks the final signal and can veto it.
        mon.message("head", "auditor", f"TAKE {setup['direction']} · confidence {verdict['confidence']}% – "
                                       f"{verdict['headline'] or verdict['reason'][:120]}", "decision")
        mon.event("Stage 5: Head Trader's signal → Signal Auditor")
        mon.agent("auditor", "thinking", summary="Checking the final signal…")
        final = verdict["levels"] or {"entry": setup["entry"], "stop_loss": setup["stop_loss"],
                                      "tps": [t["price"] for t in setup["tps"]]}
        started = time.monotonic()
        try:
            text, model = await self._ask(AUDITOR_PROMPT.format(
                instrument=ctx["instrument"], setup=ctx["setup"],
                decision=json.dumps({k: head.get(k) for k in ("decision", "confidence", "reason")}),
                levels=json.dumps(final), reports=_reports_text(leads + verifiers), min_rr=self.min_rr),
                preferred=self.slots_for("auditor"))
            audit = parse_json(text)
            approve = audit.get("approve") is True or str(audit.get("approve")).lower() == "true"
            note = str(audit.get("note", ""))[:200]
            issues = [str(x)[:150] for x in (audit.get("issues") or [])][:3]
            mon.agent("auditor", "done", vote="TAKE" if approve else "SKIP", score=100 if approve else 0,
                      summary=note, points=issues, model=model, seconds=round(time.monotonic() - started, 1))
        except Exception as e:
            # The auditor is a final safety net; if it is unreachable the desk's decision stands.
            log.warning("Signal auditor failed: %s", e)
            approve, note, issues = True, "auditor offline – desk decision stands", []
            mon.agent("auditor", "error", summary=note)
        verdict["audit"] = {"approve": approve, "note": note, "issues": issues}
        mon.message("auditor", "telegram", ("Signal approved – " if approve else "Vetoed – ")
                    + (note or "; ".join(issues)), "decision")
        verdict["approved"] = approve
        if not approve:
            verdict["reject_reason"] = "vetoed by Signal Auditor: " + ("; ".join(issues) or note)
        return verdict

    async def market_view(self, market: dict, session: dict, instrument: str = "XAU/USD (gold)") -> str:
        text, _ = await self._ask(MARKET_VIEW_PROMPT.format(market=market_brief(market), session=json.dumps(session),
                                                            instrument=instrument),
                                  preferred=self.slots_for("head"),
                                  json_mode=False)
        return text.strip()
