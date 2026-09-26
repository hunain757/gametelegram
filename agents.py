"""AI trading desk: a 4-stage pipeline of 13 Gemini agents that pass their findings on.

  Stage 1  8 analysts, each an expert in one thing, study the setup in parallel
  Stage 2  3 verifiers read ALL analyst reports and cross-check them (confluence, risk, devil's advocate)
  Stage 3  the Head Trader reads everything and decides TAKE/SKIP with final levels
  Stage 4  the Signal Auditor checks the final signal before it is sent (can veto)
"""

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types

from monitor import Monitor
from setups import TF_LABEL, validate_levels

log = logging.getLogger("goldbot.agents")

ANALYSTS = [
    {
        "key": "structure", "name": "Market Structure Analyst", "icon": "🏗",
        "focus": "Market structure across all timeframes: trend on each timeframe, BOS vs CHoCH, whether the "
                 "higher timeframes truly support this direction, and whether the entry-timeframe shift is real "
                 "or just noise inside a range.",
    },
    {
        "key": "liquidity", "name": "Liquidity & Sweep Hunter", "icon": "💧",
        "focus": "Liquidity: which pools (swing highs/lows, equal highs/lows, PDH/PDL, PWH/PWL, Asia range) were "
                 "swept before this move, which are still resting as targets, and whether the stop sits where "
                 "liquidity will be hunted.",
    },
    {
        "key": "orderblocks", "name": "Order Block & Breaker Specialist", "icon": "🧱",
        "focus": "Order blocks and breaker blocks: is the entry zone a fresh, untested OB or a valid breaker, did it "
                 "create a real break of structure, is there an opposing OB/breaker between entry and TP1?",
    },
    {
        "key": "imbalance", "name": "FVG & Imbalance Analyst", "icon": "⚡",
        "focus": "Fair value gaps and imbalances: is there an unfilled FVG supporting the entry, are opposing FVGs "
                 "in the path, and is price in premium or discount of the dealing range?",
    },
    {
        "key": "volume", "name": "Volume Profile & Order Flow Analyst", "icon": "📊",
        "focus": "Volume (PAXG/USDT, tokenized gold): relative volume, delta (buy/sell pressure), volume bubbles "
                 "(institutional candles), POC and value area. Does volume confirm this move or show absorption "
                 "against it? If volume data is unavailable, say so and judge displacement only.",
    },
    {
        "key": "price_action", "name": "Price Action & Pattern Reader", "icon": "🕯",
        "focus": "Read the candles: recent_candles on each timeframe, candlestick patterns (engulfing, pin bars, "
                 "stars, inside bars, dojis), double tops/bottoms, rejection wicks. Is there real rejection from "
                 "the entry zone or is price slicing through it?",
    },
    {
        "key": "indicators", "name": "Indicator & Momentum Analyst", "icon": "⚙️",
        "focus": "Indicators on each timeframe: EMA 20/50/200 alignment, RSI, MACD histogram, ADX trend strength "
                 "and +DI/-DI, Supertrend direction, Stochastic RSI, Bollinger Band position and VWAP. Do they "
                 "confirm the trade or warn of exhaustion / chop?",
    },
    {
        "key": "session_news", "name": "Session, News & Macro Analyst", "icon": "📰",
        "focus": "Timing and macro: session and killzone, ADR already used, the Asian range, upcoming high-impact "
                 "USD news and the latest gold/USD/Fed headlines. Is this the right time for this trade?",
    },
]

VERIFIERS = [
    {
        "key": "confluence", "name": "Confluence Verifier", "icon": "🔗",
        "focus": "Cross-check the 8 analyst reports against each other AND against the market data: do they agree, "
                 "did any analyst claim something the data does not show, which contradictions matter most?",
    },
    {
        "key": "risk", "name": "Risk Manager", "icon": "🛡",
        "focus": "Using the analysts' findings, verify the stop loss (beyond invalidation, outside stop hunts), "
                 "risk/reward of each target, volatility and whether a limit entry can fill. Protect capital first.",
    },
    {
        "key": "devil", "name": "Devil's Advocate", "icon": "😈",
        "focus": "Attack the trade using everything the analysts found: the strongest reasons it will FAIL "
                 "(trap, fake breakout, counter-trend, liquidity that will be taken against it). Vote TAKE only if "
                 "you honestly cannot find a serious flaw.",
    },
]

SPECIALISTS = ANALYSTS + VERIFIERS
HEAD = {"key": "head", "name": "Head Trader", "icon": "👑"}
AUDITOR = {"key": "auditor", "name": "Signal Auditor", "icon": "✅"}
PIPELINE = [("Stage 1 · Analysts", ANALYSTS), ("Stage 2 · Verifiers", VERIFIERS),
            ("Stage 3 · Decision", [HEAD]), ("Stage 4 · Final check", [AUDITOR])]

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

Your 8 analysts and 3 verifiers reported (after their debate):
{reports}

Desk track record for this trading style (learn from it): {history}

Make the final call. Only TAKE high-quality trades where structure, liquidity and risk line up; SKIP otherwise.
You may fine-tune entry / stop loss / targets (keep the same direction; TP1 must be at least 1:{min_rr}),
or keep the engine's levels. confidence is 0-100 and must be honest.
Reply with JSON only:
{{"decision": "TAKE" or "SKIP", "confidence": 0-100, "entry": number, "stop_loss": number,
  "tp1": number, "tp2": number, "tp3": number,
  "headline": "max 8 words, e.g. 'Sweep + H1 OB retest in discount'",
  "reason": "1-2 short sentences in simple English"}}
"""

DEBATE_PROMPT = """You are the {name} on a professional XAU/USD (gold) trading desk that trades Smart Money Concepts.
Your job: {focus}

Setup:
{setup}

Market read (per timeframe):
{market}

Your first report was: {own}

The verifiers challenge the desk:
{challenges}

Re-check the data for YOUR specialty only. Change your vote if the challenge is right, keep it if the data
supports you. Reply with JSON only:
{{"vote": "TAKE" or "SKIP", "score": 0-100, "changed": true or false,
  "reply": "one sentence answering the challenge", "summary": "your updated one-sentence view"}}
"""

VERIFIER_PROMPT = """You are the {name} on a professional XAU/USD (gold) trading desk that trades Smart Money Concepts.
Your job: {focus}

The engine proposed this setup:
{setup}

Market read (per timeframe):
{market}

Session and upcoming news: {session}

The 8 analysts sent you their reports:
{reports}

Be strict. Reply with JSON only:
{{"vote": "TAKE" or "SKIP", "score": 0-100, "summary": "one short sentence", "points": ["up to 3 short points"]}}
"""

AUDITOR_PROMPT = """You are the Signal Auditor, the last check before a XAU/USD (gold) signal is sent to traders.
Setup from the engine:
{setup}

Final decision from the head of the desk: {decision}
Final levels: {levels}

Desk reports (analysts and verifiers):
{reports}

Check that the signal is consistent: direction matches the reasons, the stop is beyond the invalidation level,
TP1 is realistic (not behind an opposing zone), R:R at least 1:{min_rr}, and nothing in the reports is a clear
reason to stop it. Veto only for a concrete problem.
Reply with JSON only:
{{"approve": true or false, "issues": ["up to 3 short issues"], "note": "one short sentence"}}
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


def _indicators(ind: dict) -> dict:
    out = {}
    for k, v in ind.items():
        if isinstance(v, dict):
            out[k] = {kk: _r(vv) for kk, vv in v.items()}
        else:
            out[k] = _r(v)
    return out


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
            "breaker_blocks": [f"{z['direction']} {z['bottom']:.2f}-{z['top']:.2f}" for z in s.get("breakers", [])[-3:]],
            "buy_side_liquidity": s["liquidity"]["buy_side"][:3],
            "sell_side_liquidity": s["liquidity"]["sell_side"][:3],
            "equal_highs": s["liquidity"]["equal_highs"][-2:],
            "equal_lows": s["liquidity"]["equal_lows"][-2:],
            "recent_sweeps": [f"{w['side']} swept @ {w['level']:.2f}" for w in s["liquidity"]["sweeps"][-2:]],
            "range": s["range"],
            "candle_patterns": [f"{x['name']}" + (f" @ {x['level']}" if "level" in x else f" ({x['age']} candles ago)")
                                for x in s.get("patterns", [])][-4:],
            "recent_candles": s.get("recent_candles", [])[-6:],
            "indicators": _indicators(ind),
            "volume": v if not v.get("available") else {k: v[k] for k in (
                "relative_volume_last_closed", "delta", "pressure", "poc", "value_area_high", "value_area_low", "bubbles")},
        }
    return json.dumps(out, indent=1)


def setup_brief(setup: dict) -> str:
    keep = ("style_label", "direction", "entry_type", "entry", "stop_loss", "tps", "price", "atr",
            "score", "confluences", "poi", "timeframes", "expiry_min")
    return json.dumps({k: setup[k] for k in keep}, indent=1)


def _reports_text(reports: list[dict]) -> str:
    return "\n".join(f"- {r['name']}: {r['vote']} ({r['score']}) - {r['summary']} {r['points']}" for r in reports)


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
        if "perday" in low or "per_day" in low or "per day" in low or "daily" in low:
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
        return "key", "Gemini key rejected – check GEMINI_API_KEY", 3600.0
    return "other", t[:140], 30.0


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
        self.last_error: dict[str, dict] = {}

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
        kind, friendly, wait = classify_error(error)
        self.cool_until[m] = time.monotonic() + wait
        self.last_error[m] = {"kind": kind, "text": friendly, "until": time.time() + wait}

    def status(self) -> list[dict]:
        """Per-model state for the dashboard."""
        now_m, now = time.monotonic(), time.time()
        out = []
        for m in self.models:
            err = self.last_error.get(m)
            cooling = self.cool_until[m] > now_m
            out.append({"model": slot_label(m), "ready": not cooling,
                        "kind": err["kind"] if err and cooling else None,
                        "reason": err["text"] if err and cooling else "",
                        "back_in_min": int(max(err["until"] - now, 0) // 60) if err and cooling else 0,
                        "calls_last_min": len(self.calls[m])})
        return out


def split_slot(slot: str) -> tuple[str, int]:
    """'gemini-3.6-flash#2' -> ('gemini-3.6-flash', 1): model name and API-key index."""
    model, _, k = slot.partition("#")
    return model, (int(k) - 1 if k else 0)


def slot_label(slot: str) -> str:
    model, k = split_slot(slot)
    return f"{model} · key {k + 1}" if "#" in slot else model


class TradingDesk:
    def __init__(self, api_key: str | list[str], model: str, min_rr: float, min_confidence: int, min_votes: int,
                 fallback_models: list[str] | None = None, rpm_per_model: int = 4):
        keys = [api_key] if isinstance(api_key, str) else [k for k in api_key if k]
        self.clients = [genai.Client(api_key=k) for k in keys]
        self.client = self.clients[0]
        models = [model] + [m for m in (fallback_models or []) if m != model]
        # Every model on every key is its own "slot" with its own free quota. Neighbouring agents get
        # different keys, so one key's limits never stop the whole desk.
        slots = [f"{m}#{k + 1}" for m in models for k in range(len(keys))] if len(keys) > 1 else models
        self.pool = ModelPool(slots, rpm_per_model)
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
                name, k = split_slot(model)
                client = self.clients[k] if hasattr(self, "clients") else self.client
                resp = await client.aio.models.generate_content(model=name, contents=prompt, config=config)
                self._count(False)
                return resp.text or "", slot_label(model)
            except Exception as e:
                self._count(True)
                last_error = e
                tried.add(model)
                self.pool.penalize(model, e)
                log.warning("Gemini %s failed: %s", model, str(e)[:120])

    async def _specialist(self, i: int, agent: dict, ctx: dict, template: str = SPECIALIST_PROMPT,
                          sources: tuple = ()) -> dict:
        mon = getattr(self, "monitor", None) or Monitor()
        mon.agent(agent["key"], "thinking", vote=None, score=None, summary="Analysing the setup…", points=[])
        started = time.monotonic()
        try:
            for src in sources:
                if isinstance(src, tuple):
                    mon.message(src[0], agent["key"], src[1])
                else:
                    mon.message(src, agent["key"])
            text, model = await self._ask(template.format(name=agent["name"], focus=agent["focus"], **ctx),
                                          preferred=self.model_for(i + 1))
            data = parse_json(text)
            vote = str(data.get("vote", "SKIP")).upper()
            report = {"key": agent["key"], "name": agent["name"], "icon": agent["icon"], "model": model,
                      "stage": 2 if agent in VERIFIERS else 1,
                      "vote": "TAKE" if vote == "TAKE" else "SKIP", "score": int(data.get("score") or 0),
                      "summary": str(data.get("summary", ""))[:200],
                      "points": [str(p)[:150] for p in (data.get("points") or [])][:3]}
            mon.agent(agent["key"], "done", vote=report["vote"], score=report["score"], summary=report["summary"],
                      points=report["points"], model=model, seconds=round(time.monotonic() - started, 1))
            mon.event(f"{agent['icon']} {agent['name']}: {report['vote']} ({report['score']}) – {report['summary']}",
                      "take" if report["vote"] == "TAKE" else "skip")
            return report
        except Exception as e:
            log.warning("%s failed: %s", agent["name"], e)
            kind, friendly, _ = classify_error(e)
            mon.agent(agent["key"], "error", summary=friendly, err=kind, seconds=round(time.monotonic() - started, 1))
            mon.event(f"{agent['icon']} {agent['name']} failed: {friendly}", "error")
            return {"key": agent["key"], "name": agent["name"], "icon": agent["icon"], "vote": "ERROR",
                    "stage": 2 if agent in VERIFIERS else 1, "score": 0, "summary": "unavailable", "points": []}

    def agent_models(self) -> dict[str, str]:
        """Which model each agent tries first."""
        keys = ["head"] + [a["key"] for a in SPECIALISTS] + ["auditor"]
        return {k: self.model_for(i) for i, k in enumerate(keys)}

    async def ping(self) -> list[dict]:
        """Health check that spends as little free quota as possible: one tiny request per MODEL
        (not per agent), then every agent is marked by the model it uses."""
        mon = self.monitor
        now = time.time()
        if now - getattr(self, "_last_ping", 0) < 90:
            mon.event("🩺 Agent test was run less than 90 s ago – please wait (it uses your free Gemini quota)", "skip")
            return []
        self._last_ping = now
        mapping = self.agent_models()
        mon.event(f"🩺 Testing the Gemini models/keys used by the {len(mapping)} agents…")
        for key in mapping:
            mon.agent(key, "thinking", summary="Health check…", vote=None, score=None, points=[], err=None)

        async def one(model: str) -> dict:
            started = time.monotonic()
            if self.pool.cool_until.get(model, 0) > time.monotonic():
                err = self.pool.last_error.get(model, {})
                return {"model": model, "ok": False, "kind": err.get("kind"), "error": err.get("text", "resting")}
            try:
                name, k = split_slot(model)
                client = self.clients[k] if hasattr(self, "clients") else self.client
                resp = await client.aio.models.generate_content(
                    model=name, contents='Reply with exactly {"ok": true}',
                    config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"))
                self._count(False)
                ok = bool(parse_json(resp.text or "").get("ok"))
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
                          summary=f"Online ✔ ({slot_label(model)} answered in {r.get('seconds')}s)")
            elif online:
                mon.agent(key, "done", vote=None, model=slot_label(online[0]), err=None,
                          summary=f"Online ✔ via backup {slot_label(online[0])} ({slot_label(model)}: {r.get('error')})")
            else:
                mon.agent(key, "error", err=r.get("kind"), summary=r.get("error") or "no model available")
        ok_agents = len(mapping) if online else 0
        mon.event(f"🩺 Models online: {len(online)}/{len(results)} → agents working: {ok_agents}/{len(mapping)}",
                  "take" if online else "error")
        if not online:
            kinds = {r.get("kind") for r in results.values()}
            if "quota_day" in kinds:
                mon.event("⛔ All Gemini models used their free daily quota. The AI desk resumes after the reset "
                          "(midnight Pacific ≈ 07:00 UTC). Engine-only signals (score ≥ ENGINE_ONLY_SCORE) still work.",
                          "error")
        return list(results.values())

    async def review(self, setup: dict, market: dict, session: dict, history: str = "",
                     practice: bool = False) -> dict:
        """Run the whole desk on one setup. Returns a verdict dict with 'approved'."""
        ctx = {"setup": setup_brief(setup), "market": market_brief(market), "session": json.dumps(session),
               "history": history}
        mon = getattr(self, "monitor", None) or Monitor()
        mon.set_phase("ai_review")
        for a in VERIFIERS + [HEAD, AUDITOR]:
            mon.agent(a["key"], "waiting", summary="Waiting for reports…", vote=None, score=None, points=[])
        mon.event(("🧪 PRACTICE review (no signal will be sent): " if practice else "🧠 AI desk reviewing ")
                  + f"{setup['style_label']} {setup['direction']} @ {setup['entry']} (engine score {setup['score']})")
        verdict = await self._review(setup, ctx, mon)
        mon.agent("head", "done" if not verdict.get("ai_down") else "error",
                  vote="TAKE" if verdict["approved"] else "SKIP", score=verdict["confidence"],
                  summary=verdict.get("reason") or verdict.get("reject_reason") or "")
        mon.event(f"👑 Head Trader: {'✅ APPROVED' if verdict['approved'] else '❌ REJECTED'} – "
                  f"{verdict.get('reject_reason') or verdict.get('headline') or verdict.get('reason')}",
                  "take" if verdict["approved"] else "skip")
        mon.review({"practice": practice, "style": setup["style_label"], "direction": setup["direction"], "entry": setup["entry"],
                    "score": setup["score"], "votes": verdict["votes"], "approved": verdict["approved"],
                    "confidence": verdict["confidence"], "reason": verdict.get("reject_reason") or verdict.get("reason"),
                    "reports": [{k: r.get(k) for k in ("icon", "name", "vote", "score", "changed")}
                                for r in verdict["reports"]]})
        mon.set_phase("idle")
        return verdict

    async def _debate(self, analysts: list[dict], verifiers: list[dict], ctx: dict, mon: Monitor):
        """Round 2: when the verifiers mostly disagree with an analyst, they send their challenge back and
        the analyst re-checks its own specialty and answers (it may change its vote)."""
        skip = [v for v in verifiers if v["vote"] == "SKIP"]
        take = [v for v in verifiers if v["vote"] == "TAKE"]
        if len(skip) >= 2:
            targets, against = [r for r in analysts if r["vote"] == "TAKE"], skip
        elif len(take) >= 2:
            targets, against = [r for r in analysts if r["vote"] == "SKIP"], take
        else:
            return
        if not targets:
            return
        mon.event(f"🗣 Debate: {len(against)} verifiers challenge {len(targets)} analysts")
        challenges = "\n".join(f"- {v['name']} ({v['vote']}): {v['summary']} {v['points']}" for v in against)
        by_key = {a["key"]: a for a in ANALYSTS}

        async def answer(r: dict):
            agent = by_key[r["key"]]
            for v in against:
                mon.message(v["key"], r["key"], f"Challenge: {v['summary']}", "challenge")
            mon.agent(r["key"], "thinking", summary="Answering the verifiers' challenge…")
            try:
                text, model = await self._ask(DEBATE_PROMPT.format(
                    name=agent["name"], focus=agent["focus"], own=f"{r['vote']} {r['score']} – {r['summary']}",
                    challenges=challenges, setup=ctx["setup"], market=ctx["market"]),
                    preferred=self.model_for(ANALYSTS.index(agent) + 1))
                d = parse_json(text)
            except Exception as e:
                kind, friendly, _ = classify_error(e)
                mon.agent(r["key"], "done", vote=r["vote"], score=r["score"], summary=r["summary"])
                mon.event(f"{agent['icon']} {agent['name']} could not answer ({friendly}); keeps {r['vote']}", "error")
                return
            old_vote = r["vote"]
            vote = "TAKE" if str(d.get("vote", old_vote)).upper() == "TAKE" else "SKIP"
            r.update(vote=vote, score=int(d.get("score") or r["score"]), debate=str(d.get("reply", ""))[:200],
                     summary=str(d.get("summary") or r["summary"])[:200], changed=vote != old_vote)
            mon.agent(r["key"], "done", vote=vote, score=r["score"], summary=r["summary"], model=model,
                      points=[f"Debate: {r['debate']}"] + r.get("points", [])[:2])
            for v in against:
                mon.message(r["key"], v["key"], f"{'Changed to ' + vote if r['changed'] else 'Keeps ' + vote}: "
                                                f"{r['debate']}", "reply")
            mon.event(f"🗣 {agent['icon']} {agent['name']}: "
                      + (f"changed {old_vote} → {vote}" if r["changed"] else f"keeps {vote}") + f" – {r['debate']}",
                      "take" if vote == "TAKE" else "skip")

        await asyncio.gather(*(answer(r) for r in targets))

    async def _review(self, setup: dict, ctx: dict, mon: Monitor) -> dict:
        # Stage 1: analysts in parallel (the engine hands each of them the setup and market read).
        mon.event("📤 Stage 1: engine → 8 analysts")
        brief = (f"{setup['style_label']} {setup['direction']} {setup['entry_type']} @ {setup['entry']} · SL "
                 f"{setup['stop_loss']} · TP1 {setup['tps'][0]['price']} · engine score {setup['score']} · "
                 + "; ".join(setup["confluences"][:3]))
        analysts = await asyncio.gather(*(self._specialist(i, a, ctx, sources=(("engine", brief),))
                                          for i, a in enumerate(ANALYSTS)))
        analysts = list(analysts)
        verdict = {"reports": list(analysts), "votes": 0, "errors": 0, "approved": False,
                   "confidence": 0, "headline": "", "reason": "", "levels": None}
        if all(r["vote"] == "ERROR" for r in analysts):
            verdict["reason"] = "AI unavailable"
            verdict["ai_down"] = True
            return verdict

        # Stage 2: verifiers receive every analyst report.
        mon.event("📤 Stage 2: analyst reports → Confluence Verifier, Risk Manager, Devil's Advocate")
        vctx = dict(ctx, reports=_reports_text(analysts))
        shared = tuple((r["key"], f"{r['vote']} {r['score']} – {r['summary']}") for r in analysts)
        verifiers = await asyncio.gather(*(self._specialist(len(ANALYSTS) + i, v, vctx, VERIFIER_PROMPT, shared)
                                           for i, v in enumerate(VERIFIERS)))
        if getattr(self, "debate", True):
            await self._debate(analysts, list(verifiers), ctx, mon)
        reports = list(analysts) + list(verifiers)
        a_votes = sum(r["vote"] == "TAKE" for r in analysts)
        v_votes = sum(r["vote"] == "TAKE" for r in verifiers)
        verdict.update(reports=reports, votes=a_votes + v_votes, analyst_votes=a_votes, verifier_votes=v_votes,
                       errors=sum(r["vote"] == "ERROR" for r in reports))

        # Stage 3: the Head Trader reads everything.
        for r in reports:
            mon.message(r["key"], "head", f"{r['vote']} {r['score']} – {r['summary']}")
        mon.event("📤 Stage 3: all 11 reports → 👑 Head Trader")
        mon.agent("head", "thinking", summary="Reading all 11 reports and making the final call…")
        try:
            text, _ = await self._ask(HEAD_PROMPT.format(reports=_reports_text(reports), min_rr=self.min_rr,
                                                         history=ctx.get("history") or "no closed trades yet",
                                                         **{k: v for k, v in ctx.items() if k != "history"}),
                                      preferred=self.model_for(0))
            head = parse_json(text)
        except Exception as e:
            # Reports did arrive, so this is not "AI down": decide on the votes alone, with a clear majority.
            log.warning("Head trader failed: %s", e)
            takers = [r["score"] for r in reports if r["vote"] == "TAKE"]
            verdict["confidence"] = round(sum(takers) / len(takers)) if takers else 0
            verdict["reason"] = "Head trader offline – decided by the desk's votes."
            need = max(self.min_votes, -(-3 * len(ANALYSTS) // 4))
            verdict["approved"] = a_votes >= need and v_votes >= 2 and verdict["confidence"] >= self.min_confidence
            if not verdict["approved"]:
                verdict["reject_reason"] = f"head trader offline and only {a_votes}/{len(ANALYSTS)} analysts agree"
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

        reasons = []
        if not take:
            reasons.append("head trader said SKIP")
        if verdict["confidence"] < self.min_confidence:
            reasons.append(f"confidence {verdict['confidence']} < {self.min_confidence}")
        if a_votes < self.min_votes:
            reasons.append(f"only {a_votes}/{len(ANALYSTS)} analysts agree")
        if v_votes < 2:
            reasons.append(f"only {v_votes}/{len(VERIFIERS)} verifiers agree")
        if reasons:
            verdict["reject_reason"] = "; ".join(reasons)
            return verdict

        # Stage 4: the Signal Auditor checks the final signal and can veto it.
        mon.message("head", "auditor", f"TAKE {setup['direction']} · confidence {verdict['confidence']}% – "
                                       f"{verdict['headline'] or verdict['reason'][:120]}", "decision")
        mon.event("📤 Stage 4: Head Trader's signal → ✅ Signal Auditor")
        mon.agent("auditor", "thinking", summary="Checking the final signal…")
        final = verdict["levels"] or {"entry": setup["entry"], "stop_loss": setup["stop_loss"],
                                      "tps": [t["price"] for t in setup["tps"]]}
        started = time.monotonic()
        try:
            text, model = await self._ask(AUDITOR_PROMPT.format(
                setup=ctx["setup"], decision=json.dumps({k: head.get(k) for k in ("decision", "confidence", "reason")}),
                levels=json.dumps(final), reports=_reports_text(reports), min_rr=self.min_rr),
                preferred=self.model_for(len(SPECIALISTS) + 1))
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
        mon.message("auditor", "telegram", ("✅ Signal approved – " if approve else "⛔ Vetoed – ")
                    + (note or "; ".join(issues)), "decision")
        verdict["approved"] = approve
        if not approve:
            verdict["reject_reason"] = "vetoed by Signal Auditor: " + ("; ".join(issues) or note)
        return verdict

    async def market_view(self, market: dict, session: dict) -> str:
        text, _ = await self._ask(MARKET_VIEW_PROMPT.format(market=market_brief(market), session=json.dumps(session)),
                                  json_mode=False)
        return text.strip()
