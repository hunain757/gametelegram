"""AI trading desk: eight specialist Gemini agents review a setup in parallel,
then a Head Trader agent reads their reports and makes the final call (9 agents in total)."""

import asyncio
import json
import logging
import re
import time
from collections import deque

from google import genai
from google.genai import types

from setups import TF_LABEL, validate_levels

log = logging.getLogger("goldbot.agents")

SPECIALISTS = [
    {
        "key": "structure", "name": "Market Structure Analyst", "icon": "🏗",
        "focus": "Market structure across all timeframes: trend on each timeframe, BOS vs CHoCH, whether the "
                 "higher timeframes truly support this direction, and whether the entry-timeframe shift is real "
                 "or just noise inside a range.",
    },
    {
        "key": "liquidity", "name": "Liquidity & Order Block Hunter", "icon": "💧",
        "focus": "Smart-money footprint: was liquidity (swing lows/highs, equal highs/lows, PDH/PDL, Asia range) "
                 "swept before the move, is the order block / fair value gap fresh and valid, is there opposing "
                 "liquidity or an opposing order block in the path, and are the targets at real liquidity pools.",
    },
    {
        "key": "volume", "name": "Volume & Order Flow Analyst", "icon": "📊",
        "focus": "Volume (from PAXG/USDT, tokenized gold): relative volume, buy/sell pressure (delta), the "
                 "volume spike candle, POC and value area. Does volume confirm this move or show absorption against "
                 "it? If volume data is unavailable, judge only from displacement and say so.",
    },
    {
        "key": "price_action", "name": "Price Action & Chart Pattern Reader", "icon": "🕯",
        "focus": "Read the candles like a chart: the last candles on each timeframe (recent_candles), candlestick "
                 "patterns (engulfing, pin bars, stars, inside bars, dojis), double tops/bottoms, rejection wicks, "
                 "and whether the chart shows real rejection from the entry zone or price slicing through it.",
    },
    {
        "key": "momentum", "name": "Momentum & Indicator Analyst", "icon": "⚙️",
        "focus": "EMA 20/50/200 alignment on each timeframe, RSI (overbought/oversold, divergence risk), MACD "
                 "histogram and volatility (ATR). Is momentum with the trade, turning, or exhausted?",
    },
    {
        "key": "session_news", "name": "Session, Timing & News Analyst", "icon": "🕐",
        "focus": "Timing: current session and killzone, how much of the average daily range (ADR) is already used, "
                 "the Asian range, and upcoming high-impact news. Is this a good time to open this trade and can it "
                 "reach its targets before the session / news changes the market?",
    },
    {
        "key": "risk", "name": "Risk Manager", "icon": "🛡",
        "focus": "Stop-loss placement (beyond the invalidation level and outside obvious stop hunts?), risk/reward "
                 "of each target, volatility, and whether a limit entry is realistic to fill before expiry. "
                 "Protect capital first.",
    },
    {
        "key": "devil", "name": "Devil's Advocate", "icon": "😈",
        "focus": "Your job is to attack this trade. Find the strongest reasons it will FAIL: a trap, a fake "
                 "breakout, counter-trend risk, a better opposite setup, liquidity that will be taken against it. "
                 "Vote TAKE only if you honestly cannot find a serious flaw.",
    },
]

SPECIALIST_PROMPT = """You are the {name} on a professional XAU/USD (gold) trading desk that trades Smart Money Concepts.
Your job: {focus}

A rule-based engine proposed this setup:
{setup}

Current market read (per timeframe):
{market}

Session and upcoming news: {session}

Judge ONLY from your specialty and be strict: say SKIP if your part of the picture is weak.
Reply with JSON only:
{{"vote": "TAKE" or "SKIP", "score": 0-100, "summary": "one short sentence", "points": ["up to 3 short points"]}}
"""

HEAD_PROMPT = """You are the Head Trader of a professional XAU/USD (gold) desk that trades Smart Money Concepts.
The engine proposed this setup:
{setup}

Market read (per timeframe):
{market}

Session and upcoming news: {session}

Your eight specialists reported:
{reports}

Make the final call. Only TAKE high-quality trades where structure, liquidity and risk line up; SKIP otherwise.
You may fine-tune entry / stop loss / targets (keep the same direction; TP1 must be at least 1:{min_rr}),
or keep the engine's levels. confidence is 0-100 and must be honest.
Reply with JSON only:
{{"decision": "TAKE" or "SKIP", "confidence": 0-100, "entry": number, "stop_loss": number,
  "tp1": number, "tp2": number, "tp3": number,
  "headline": "max 8 words, e.g. 'Sweep + H1 OB retest in discount'",
  "reason": "1-2 short sentences in simple English"}}
"""

MARKET_VIEW_PROMPT = """You are a senior XAU/USD (gold) analyst who trades Smart Money Concepts.
Market read (per timeframe):
{market}

Session: {session}

Write a short market outlook for traders on Telegram (max 90 words, simple English, no markdown symbols):
overall bias per timeframe, key liquidity above and below, the nearest order blocks / FVGs to watch,
and what would make you buy or sell. Do not invent prices that are not in the data.
"""


def _r(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) else x


def market_brief(market: dict) -> str:
    """Compact JSON of the per-timeframe read, for prompts."""
    out = {"key_levels": market.get("levels", {}), "average_daily_range": market.get("adr", {})}
    for tf, m in market.items():
        if tf in ("levels", "adr"):
            continue
        s, v, ind = m["smc"], m["volume"], m["ind"]
        out[TF_LABEL.get(tf, tf)] = {
            "price": _r(m["price"]),
            "atr": _r(s["atr"]),
            "trend": s["trend"],
            "structure_events": [f"{e['type']} {e['direction']} @ {e['level']:.2f}" for e in s["events"][-3:]],
            "order_blocks": [f"{z['direction']} {z['bottom']:.2f}-{z['top']:.2f}{' (tested)' if z['touched'] else ''}"
                             for z in s["order_blocks"][-3:]],
            "fvgs": [f"{z['direction']} {z['bottom']:.2f}-{z['top']:.2f}" for z in s["fvgs"][-3:]],
            "buy_side_liquidity": s["liquidity"]["buy_side"][:3],
            "sell_side_liquidity": s["liquidity"]["sell_side"][:3],
            "equal_highs": s["liquidity"]["equal_highs"][-2:],
            "equal_lows": s["liquidity"]["equal_lows"][-2:],
            "recent_sweeps": [f"{w['side']} swept @ {w['level']:.2f}" for w in s["liquidity"]["sweeps"][-2:]],
            "range": s["range"],
            "candle_patterns": [f"{x['name']}" + (f" @ {x['level']}" if "level" in x else f" ({x['age']} candles ago)")
                                for x in s.get("patterns", [])][-4:],
            "recent_candles": s.get("recent_candles", [])[-6:],
            "indicators": {k: _r(val) for k, val in ind.items()},
            "volume": v if not v.get("available") else {k: v[k] for k in (
                "relative_volume_last_closed", "delta", "pressure", "poc", "value_area_high", "value_area_low")},
        }
    return json.dumps(out, indent=1)


def setup_brief(setup: dict) -> str:
    keep = ("style_label", "direction", "entry_type", "entry", "stop_loss", "tps", "price", "atr",
            "score", "confluences", "poi", "timeframes", "expiry_min")
    return json.dumps({k: setup[k] for k in keep}, indent=1)


def parse_json(text: str) -> dict:
    """Parse the model's JSON, tolerating ```json fences around it."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError(f"AI did not return JSON: {(text or '')[:200]}")
    return json.loads(match.group(0))


class ModelPool:
    """Spreads calls over several Gemini models and respects the free plan's per-model rate limit.

    The free tier allows only a few requests per minute *per model*, so each agent gets its
    own model, and a busy / rate-limited / overloaded model is skipped in favour of the next.
    """

    def __init__(self, models: list[str], rpm: int = 4):
        self.models = models
        self.rpm = rpm
        self.calls = {m: deque() for m in models}
        self.cool_until = {m: 0.0 for m in models}

    def _ready_at(self, m: str, now: float) -> float:
        q = self.calls[m]
        while q and now - q[0] > 60:
            q.popleft()
        slot = q[0] + 60 if len(q) >= self.rpm else now
        return max(slot, self.cool_until[m])

    async def acquire(self, preferred: str, tried: set) -> str | None:
        """Next usable model. Waits only for a rate-limit slot, never for a model that is erroring."""
        order = [preferred] + [m for m in self.models if m != preferred]
        while True:
            now = time.monotonic()
            left = [m for m in order if m not in tried and self.cool_until[m] <= now]
            if not left:
                return None
            for m in left:
                if self._ready_at(m, now) <= now:
                    self.calls[m].append(now)
                    return m
            await asyncio.sleep(min(max(min(self._ready_at(m, now) for m in left) - now, 0.5), 65))

    def penalize(self, m: str, error: Exception):
        text = str(error)
        wait = 86400 if "404" in text else 60 if "429" in text else 30
        self.cool_until[m] = time.monotonic() + wait


class TradingDesk:
    def __init__(self, api_key: str, model: str, min_rr: float, min_confidence: int, min_votes: int,
                 fallback_models: list[str] | None = None, rpm_per_model: int = 4):
        self.client = genai.Client(api_key=api_key)
        self.pool = ModelPool([model] + [m for m in (fallback_models or []) if m != model], rpm_per_model)
        self.min_rr = min_rr
        self.min_confidence = min_confidence
        self.min_votes = min_votes
        self.usage = {"day": None, "calls": 0, "failures": 0}

    def _count(self, failed: bool):
        from datetime import date
        if self.usage["day"] != date.today():
            self.usage.update(day=date.today(), calls=0, failures=0)
        self.usage["calls"] += 1
        self.usage["failures"] += failed

    def model_for(self, i: int) -> str:
        """Head trader uses the main model; specialists are spread over the others."""
        models = self.pool.models
        return models[i % len(models)]

    async def _ask(self, prompt: str, preferred: str | None = None, json_mode: bool = True) -> tuple[str, str]:
        """Returns (text, model used)."""
        config = types.GenerateContentConfig(
            temperature=0.2, response_mime_type="application/json" if json_mode else "text/plain")
        tried, last_error = set(), None
        while True:
            model = await self.pool.acquire(preferred or self.pool.models[0], tried)
            if model is None:
                raise last_error or RuntimeError("no Gemini model available")
            try:
                resp = await self.client.aio.models.generate_content(model=model, contents=prompt, config=config)
                self._count(False)
                return resp.text or "", model
            except Exception as e:
                self._count(True)
                last_error = e
                tried.add(model)
                self.pool.penalize(model, e)
                log.warning("Gemini %s failed: %s", model, str(e)[:120])

    async def _specialist(self, i: int, agent: dict, ctx: dict) -> dict:
        try:
            text, model = await self._ask(SPECIALIST_PROMPT.format(name=agent["name"], focus=agent["focus"], **ctx),
                                          preferred=self.model_for(i + 1))
            data = parse_json(text)
            vote = str(data.get("vote", "SKIP")).upper()
            return {"key": agent["key"], "name": agent["name"], "icon": agent["icon"], "model": model,
                    "vote": "TAKE" if vote == "TAKE" else "SKIP", "score": int(data.get("score") or 0),
                    "summary": str(data.get("summary", ""))[:200],
                    "points": [str(p)[:150] for p in (data.get("points") or [])][:3]}
        except Exception as e:
            log.warning("%s failed: %s", agent["name"], e)
            return {"key": agent["key"], "name": agent["name"], "icon": agent["icon"], "vote": "ERROR",
                    "score": 0, "summary": "unavailable", "points": []}

    async def review(self, setup: dict, market: dict, session: dict) -> dict:
        """Run the whole desk on one setup. Returns a verdict dict with 'approved'."""
        ctx = {"setup": setup_brief(setup), "market": market_brief(market), "session": json.dumps(session)}
        reports = await asyncio.gather(*(self._specialist(i, a, ctx) for i, a in enumerate(SPECIALISTS)))
        votes = sum(r["vote"] == "TAKE" for r in reports)
        errors = sum(r["vote"] == "ERROR" for r in reports)
        verdict = {"reports": reports, "votes": votes, "errors": errors, "approved": False,
                   "confidence": 0, "headline": "", "reason": "", "levels": None}
        if errors == len(reports):
            verdict["reason"] = "AI unavailable"
            verdict["ai_down"] = True
            return verdict

        reports_text = "\n".join(f"- {r['name']}: {r['vote']} ({r['score']}) - {r['summary']} {r['points']}"
                                 for r in reports)
        try:
            text, _ = await self._ask(HEAD_PROMPT.format(reports=reports_text, min_rr=self.min_rr, **ctx),
                                      preferred=self.model_for(0))
            head = parse_json(text)
        except Exception as e:
            # Specialists did answer, so this is not "AI down": decide on their votes alone,
            # and only when a clear majority says TAKE.
            log.warning("Head trader failed: %s", e)
            takers = [r["score"] for r in reports if r["vote"] == "TAKE"]
            verdict["confidence"] = round(sum(takers) / len(takers)) if takers else 0
            verdict["reason"] = "Head trader offline – decided by specialist votes."
            need = max(self.min_votes, -(-3 * len(reports) // 4))  # a clear 75% majority without the head
            verdict["approved"] = votes >= need and verdict["confidence"] >= self.min_confidence
            if not verdict["approved"]:
                verdict["reject_reason"] = f"head trader offline and only {votes}/{len(reports)} agents agree"
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

        verdict["approved"] = take and verdict["confidence"] >= self.min_confidence and votes >= self.min_votes
        if not verdict["approved"]:
            verdict["reject_reason"] = (
                "head trader said SKIP" if not take else
                f"confidence {verdict['confidence']} < {self.min_confidence}"
                if verdict["confidence"] < self.min_confidence else f"only {votes}/{len(reports)} agents agree")
        return verdict

    async def market_view(self, market: dict, session: dict) -> str:
        text, _ = await self._ask(MARKET_VIEW_PROMPT.format(market=market_brief(market), session=json.dumps(session)),
                                  json_mode=False)
        return text.strip()
