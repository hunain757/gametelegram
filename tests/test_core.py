import asyncio
import json
import math
import os
import random
import re
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone

import setups
import smc
import tracker
import labels
from agents import TradingDesk, parse_json
from indicators import atr, ema, macd, rsi
from sessions import is_market_open
from setups import analyze_market, find_setup, pick_targets, validate_levels
from storage import Storage, stats

# Test scenarios use random data; strict mode is tested on its own below.
os.environ.setdefault("STRICT_MODE", "off")
os.environ.setdefault("MARKETS", "XAUUSD")  # single market unless a test adds BTC itself

SESSION = {"sessions": ["London"], "killzone": "London Open", "utc_time": "08:00"}


def candle(t, o, h, l, c, v=100.0):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


def zigzag(points, steps=5):
    """Candles that walk linearly between the given closes."""
    out, i = [], 0
    for a, b in zip(points, points[1:]):
        for k in range(steps):
            o = a + (b - a) * k / steps
            c = a + (b - a) * (k + 1) / steps
            out.append(candle(f"2026-09-2{1 + i // 1000} {i // 60 % 24:02d}:{i % 60:02d}:00", o,
                              max(o, c) + 0.2, min(o, c) - 0.2, c))
            i += 1
    return out


def random_market(seed):
    vols = [("5min", 0.8), ("15min", 1.4), ("1h", 2.8), ("4h", 5.5), ("1day", 14)]
    minutes = {"5min": 5, "15min": 15, "1h": 60, "4h": 240, "1day": 1440}
    end = datetime(2026, 9, 23, 10, 0)
    data = {}
    for k, (tf, vol) in enumerate(vols):
        rnd, price, out = random.Random(seed * 7 + k), 2650.0, []
        t0 = end - timedelta(minutes=minutes[tf] * 300)
        drift = 0.0
        for i in range(300):
            if i % 60 == 0:
                drift = rnd.choice([-1, 1]) * vol * rnd.uniform(0.02, 0.12)
            o = price
            c = o + drift + rnd.gauss(0, vol)
            out.append(candle((t0 + timedelta(minutes=minutes[tf] * i)).strftime("%Y-%m-%d %H:%M:%S"), o, max(o, c) + abs(rnd.gauss(0, vol * 0.6)),
                              min(o, c) - abs(rnd.gauss(0, vol * 0.6)), c, abs(rnd.gauss(100, 40))))
            price = c
        data[tf] = out
    last = data["5min"][-1]["close"]
    for c_list in data.values():
        shift = last - c_list[-1]["close"]
        for x in c_list:
            for key in ("open", "high", "low", "close"):
                x[key] += shift
    return data


def first_setup(max_seed=200):
    for seed in range(max_seed):
        data = random_market(seed)
        market = analyze_market(data, {tf: [dict(c) for c in cs] for tf, cs in data.items()})
        for style in setups.STYLES:
            s = find_setup(style, market, SESSION, 1.5)
            if s:
                return s, market, data
    raise AssertionError("no setup found")


class IndicatorTests(unittest.TestCase):
    def test_ema_constant_series(self):
        self.assertAlmostEqual(ema([5.0] * 30, 10)[-1], 5.0)
        self.assertIsNone(ema([1.0, 2.0], 10)[-1])

    def test_rsi_bounds(self):
        self.assertEqual(rsi([float(i) for i in range(30)]), 100.0)
        self.assertLess(rsi([float(30 - i) for i in range(30)]), 1)

    def test_atr_and_macd(self):
        cs = [candle(str(i), 1, 2 + math.sin(i), 0, 1.5) for i in range(60)]
        self.assertGreater(atr(cs), 0)
        self.assertIsNotNone(macd([c["close"] for c in cs]))


class SMCTests(unittest.TestCase):
    def test_uptrend_structure_and_order_block(self):
        # Higher highs and higher lows: 100 -> 110 -> 105 -> 115 -> 108 -> 120
        cs = zigzag([100, 110, 105, 115, 108, 120, 117])
        swings = smc.find_swings(cs)
        self.assertTrue(any(s["type"] == "high" and abs(s["price"] - 110.2) < 0.01 for s in swings))
        st = smc.market_structure(cs, swings)
        self.assertEqual(st["trend"], "bullish")
        self.assertIn("BOS", [e["type"] for e in st["events"]])
        obs = smc.order_blocks(cs, st["events"])
        self.assertTrue(obs and all(o["direction"] == "bullish" for o in obs))

    def test_choch_on_reversal(self):
        cs = zigzag([100, 110, 105, 115, 108, 120, 112, 116, 100])
        st = smc.market_structure(cs, smc.find_swings(cs))
        self.assertEqual(st["trend"], "bearish")
        self.assertEqual([e for e in st["events"] if e["direction"] == "bearish"][0]["type"], "CHoCH")

    def test_fair_value_gap(self):
        cs = [candle("1", 100, 101, 99, 100.5), candle("2", 100.5, 105, 100.4, 104.8),
              candle("3", 104.8, 106, 102, 105.5), candle("4", 105.5, 106, 103, 105)]
        gaps = smc.fair_value_gaps(cs)
        self.assertEqual(gaps[0]["direction"], "bullish")
        self.assertEqual((gaps[0]["bottom"], gaps[0]["top"]), (101, 102))

    def test_sell_side_sweep(self):
        cs = zigzag([110, 100, 108, 104, 107], steps=4)
        cs.append(candle("sweep", 105, 106, 99.0, 105.5))  # wick under the 99.8 low, close back above
        liq = smc.liquidity(cs, smc.find_swings(cs), tolerance=0.3)
        self.assertTrue(any(s["direction"] == "bullish" for s in liq["sweeps"]))


class SetupTests(unittest.TestCase):
    def test_pick_targets_prefers_liquidity(self):
        tps = pick_targets(100, 2, [104, 109, 130], bull=True, min_rr=1.5)
        self.assertEqual([t["price"] for t in tps[:2]], [104, 109])
        self.assertEqual(tps[0]["source"], "liquidity")
        self.assertTrue(tps[0]["price"] < tps[1]["price"] < tps[2]["price"])

    def test_pick_targets_fallback(self):
        tps = pick_targets(100, 2, [], bull=False, min_rr=1.5)
        self.assertEqual(tps[0]["price"], 97)
        self.assertTrue(all(t["source"] == "R-multiple" for t in tps))

    def test_validate_levels(self):
        self.assertTrue(validate_levels("BUY", 100, 98, [103, 105, 108], 100.2, 2, 1.5, "MARKET")[0])
        self.assertFalse(validate_levels("BUY", 100, 98, [102, 105, 108], 100.2, 2, 1.5, "MARKET")[0])
        self.assertFalse(validate_levels("SELL", 100, 98, [97, 95, 92], 100, 2, 1.5, "MARKET")[0])
        self.assertFalse(validate_levels("BUY", 100, 98, [103, 105, 108], 104, 2, 1.5, "MARKET")[0])
        self.assertTrue(validate_levels("SELL", 101, 103, [98, 96, 94], 100, 2, 1.5, "LIMIT")[0])

    def test_engine_setups_are_always_valid(self):
        found = 0
        for seed in range(60):
            data = random_market(seed)
            market = analyze_market(data)
            for style in setups.STYLES:
                s = find_setup(style, market, SESSION, 1.5)
                if s:
                    found += 1
                    ok, why = validate_levels(s["direction"], s["entry"], s["stop_loss"],
                                              [t["price"] for t in s["tps"]], s["price"], s["atr"], 1.5,
                                              s["entry_type"])
                    self.assertTrue(ok, why)
                    self.assertTrue(0 <= s["score"] <= 100)
        self.assertGreater(found, 0)


def make_trade(direction="BUY", entry_type="LIMIT", entry=100.0, sl=98.0, tps=(103.0, 105.0, 108.0), expiry=60):
    setup = {"style": "intraday", "style_label": "📊 Intraday", "direction": direction, "entry_type": entry_type,
             "entry": entry, "stop_loss": sl, "tps": [{"price": p} for p in tps], "price": entry + 0.5,
             "score": 70, "confluences": ["x"], "expiry_min": expiry,
             "timeframes": {"entry": "15min", "confirm": "1h", "bias": "4h"}}
    now = datetime(2026, 9, 22, 8, tzinfo=timezone.utc)
    return tracker.new_trade(setup, {"confidence": 80, "votes": 4, "reports": []}, "2026-09-22 08:00:00", now), now


def c5(minute, low, high):
    return candle(f"2026-09-22 08:{minute:02d}:00", (low + high) / 2, high, low, (low + high) / 2)


class TrackerTests(unittest.TestCase):
    def test_full_buy_lifecycle(self):
        t, now = make_trade()
        self.assertEqual(t["status"], "pending")
        kinds = [e["kind"] for e in tracker.update(t, [c5(5, 99.8, 100.6)], now)]
        self.assertEqual(kinds, ["filled"])
        ev = tracker.update(t, [c5(5, 99.8, 100.6), c5(10, 100.5, 103.2)], now)
        self.assertEqual((ev[0]["kind"], ev[0]["n"], ev[0]["new_sl"]), ("tp", 1, 100.0))
        ev = tracker.update(t, [c5(15, 102, 105.5)], now)
        self.assertEqual(ev[0]["n"], 2)
        self.assertEqual(t["stop_loss"], 103.0)
        ev = tracker.update(t, [c5(20, 102.5, 104)], now)
        self.assertEqual(ev[0]["kind"], "protected_stop")
        # 1/3 at TP1 (1.5R) + 1/3 at TP2 (2.5R) + last 1/3 stopped at TP1 (1.5R)
        self.assertEqual((t["status"], t["outcome"], t["result_r"]), ("closed", "win", 1.83))

    def test_sell_market_stop_loss(self):
        t, now = make_trade("SELL", "MARKET", entry=100, sl=102, tps=(97, 95, 92))
        self.assertEqual(t["status"], "active")
        ev = tracker.update(t, [c5(5, 99, 102.3)], now)
        self.assertEqual(ev[0]["kind"], "sl")
        self.assertEqual((t["outcome"], t["result_r"]), ("loss", -1))

    def test_stop_before_target_in_same_candle(self):
        t, now = make_trade(entry_type="MARKET")
        ev = tracker.update(t, [c5(5, 97.5, 103.5)], now)
        self.assertEqual(ev[0]["kind"], "sl")

    def test_expiry_and_cancel(self):
        t, now = make_trade(expiry=30)
        tracker.update(t, [c5(5, 100.5, 101)], now + timedelta(minutes=31))
        self.assertEqual(t["outcome"], "expired")
        t, now = make_trade()
        ev = tracker.update(t, [c5(5, 100.5, 103.5)], now)
        self.assertEqual(ev[0]["kind"], "cancelled")

    def test_old_candles_ignored(self):
        t, now = make_trade(entry_type="MARKET")
        self.assertEqual(tracker.update(t, [candle("2026-09-22 07:55:00", 90, 120, 80, 100)], now), [])


class StorageTests(unittest.TestCase):
    def test_migration_and_prefs(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "data.json")
            with open(path, "w") as f:
                json.dump({"subscribers": [42], "last_signal": None}, f)
            old = datetime.now(timezone.utc) - timedelta(days=5)
            with open(path, "w") as f:  # a file written by the old Telegram version
                json.dump({"subscribers": [42], "users": {"42": {}}, "owner": 42, "last_signal": None,
                           "trades": [{"id": "a", "status": "closed", "messages": {"42": 7}}],
                           "seen": {"old": old.isoformat()}, "style_settings": {"scalp": {"enabled": False}}}, f)
            s = Storage(path)
            self.assertEqual(set(s.data), {"trades", "seen", "style_settings", "account", "ai_pool"})
            self.assertNotIn("messages", s.data["trades"][0])
            self.assertEqual(s.data["seen"], {})
            self.assertFalse(s.style_settings("scalp")["enabled"])
            with open(path) as f:
                self.assertNotIn("subscribers", json.load(f))  # the cleaned file was written back
            s.set_account(balance=1000, risk=2)
            self.assertEqual(Storage(path).account, {"balance": 1000, "risk": 2})

    def test_stats(self):
        trades = [{"style": "scalp", "outcome": "win", "stage": 2, "result_r": 2.5},
                  {"style": "scalp", "outcome": "loss", "stage": 0, "result_r": -1},
                  {"style": "swing", "outcome": "expired", "stage": 0, "result_r": 0}]
        s = stats(trades)
        self.assertEqual((s["trades"], s["wins"], s["win_rate"], s["total_r"], s["expired"]), (2, 1, 50, 1.5, 1))
        self.assertEqual((s["profit_factor"], s["max_dd"]), (2.5, 1))
        self.assertEqual([p["r"] for p in s["equity"]], [2.5, 1.5])


class MarketHoursTests(unittest.TestCase):
    def test_hours(self):
        utc = timezone.utc
        self.assertTrue(is_market_open(datetime(2026, 9, 23, 12, tzinfo=utc)))       # Wednesday
        self.assertFalse(is_market_open(datetime(2026, 9, 26, 12, tzinfo=utc)))      # Saturday
        self.assertFalse(is_market_open(datetime(2026, 9, 25, 21, 30, tzinfo=utc)))  # Friday late
        self.assertTrue(is_market_open(datetime(2026, 9, 27, 22, 30, tzinfo=utc)))   # Sunday open


class FakeModels:
    def __init__(self, head_decision="TAKE", fail=False, veto=False):
        self.head_decision, self.fail, self.calls, self.veto = head_decision, fail, 0, veto

    async def generate_content(self, model, contents, config):
        self.calls += 1
        if self.fail:
            raise RuntimeError("quota")
        if "Signal Auditor" in contents:
            return types.SimpleNamespace(text=json.dumps({"approve": not self.veto, "issues": ["SL inside PDL pool"],
                                                          "note": "consistent" if not self.veto else "stop too tight"}))
        if "Head Trader" in contents:
            return types.SimpleNamespace(text=json.dumps({
                "decision": self.head_decision, "confidence": 81, "entry": "bad", "headline": "Sweep + OB retest",
                "reason": "All desks aligned.", "explain": "Price swept the lows and is back in the order block.",
                "for": ["H1 bullish BOS @ 2617.9", "sell-side swept @ 2613.0"], "against": ["ADR 80% used"],
                "invalidation": "A close below 2605 ends the idea.", "management": "Move stop to entry at TP1.",
                "risks": ["CPI tomorrow"]}))
        return types.SimpleNamespace(text='```json\n{"vote": "TAKE", "score": 75, "summary": "data supports the trade", '
                                          '"points": ["H1 bullish BOS @ 2617.9", "sell-side swept @ 2613.0"]}\n```')


def fake_desk(**kw):
    from agents import ModelPool
    desk = TradingDesk.__new__(TradingDesk)
    desk.pool, desk.min_rr, desk.min_confidence, desk.min_votes = ModelPool(["m1", "m2"], rpm=50), 1.5, 70, 3
    desk.pool.max_wait = 0  # tests never wait for a resting slot
    desk.usage = {"day": None, "calls": 0, "failures": 0}
    from monitor import Monitor
    desk.monitor = Monitor()
    desk.fake = FakeModels(**kw)
    desk.client = types.SimpleNamespace(aio=types.SimpleNamespace(models=desk.fake))
    return desk


class AgentTests(unittest.TestCase):
    def test_parse_json_with_fences(self):
        self.assertEqual(parse_json('```json\n{"trade": false}\n```'), {"trade": False})

    def test_desk_approves_and_keeps_engine_levels(self):
        setup, market, _ = first_setup()
        desk = fake_desk()
        v = asyncio.run(desk.review(setup, market, SESSION))
        self.assertTrue(v["approved"])
        self.assertEqual(v["votes"], 24)       # 18 analysts + 3 desk leads + 3 verifiers
        self.assertEqual(v["per_desk"], {"tech": "9/9", "strategy": "5/5", "macro": "4/4"})
        self.assertTrue(v["audit"]["approve"])
        self.assertIsNone(v["levels"])  # head trader's levels were invalid -> engine levels
        self.assertEqual(desk.fake.calls, 26)  # every agent exactly once (no debate when all agree)
        flows = list(desk.monitor.flows)
        self.assertTrue(any(f["from"] == "structure" and f["to"] == "tech_lead" for f in flows))
        self.assertTrue(any(f["from"] == "world" and f["to"] == "macro_lead" for f in flows))
        self.assertTrue(any(f["from"] == "strategy_lead" and f["to"] == "confluence" for f in flows))
        self.assertTrue(any(f["from"] == "head" and f["to"] == "auditor" for f in flows))
        self.assertEqual(v["invalidation"], "A close below 2605 ends the idea.")
        self.assertEqual(len(v["for"]), 2)
        t = tracker.new_trade(dict(setup, symbol_name="XAU/USD"), v, "2026-09-22 08:00:00")
        self.assertIn("swept the lows", t["explain"])
        self.assertEqual(t["invalidation"], "A close below 2605 ends the idea.")
        self.assertEqual(len(t["evidence_for"]), 2)
        cur = desk.monitor.current  # live-review panel: which setup, when it started/ended and the outcome
        self.assertIn("swept", cur["explain"])
        self.assertIn(setup["direction"], cur["label"])
        self.assertTrue(cur["approved"])
        self.assertGreaterEqual(cur["end"], cur["ts"])

    def test_auditor_can_veto(self):
        setup, market, _ = first_setup()
        v = asyncio.run(fake_desk(veto=True).review(setup, market, SESSION))
        self.assertFalse(v["approved"])
        self.assertIn("Signal Auditor", v["reject_reason"])

    def test_desk_skip_and_down(self):
        setup, market, _ = first_setup()
        self.assertFalse(asyncio.run(fake_desk(head_decision="SKIP").review(setup, market, SESSION))["approved"])
        v = asyncio.run(fake_desk(fail=True).review(setup, market, SESSION))
        self.assertTrue(v.get("ai_down"))


class DebateTests(unittest.TestCase):
    def test_leads_challenge_and_analysts_answer(self):
        setup, market, _ = first_setup()
        desk = fake_desk()

        class Models:
            async def generate_content(self, model, contents, config):
                if "Your desk lead challenges you" in contents:  # debate answer: analyst changes its mind
                    return types.SimpleNamespace(text='{"vote": "SKIP", "score": 40, "changed": true, '
                                                      '"reply": "Agreed, ADR is exhausted", "summary": "now SKIP"}')
                if "Your members and their jobs" in contents or "three desk leads" in contents:  # leads/verifiers SKIP
                    return types.SimpleNamespace(text='{"vote": "SKIP", "score": 30, "summary": "ADR exhausted", "points": []}')
                if "Head Trader" in contents:
                    return types.SimpleNamespace(text='{"decision": "SKIP", "confidence": 20, "reason": "no"}')
                return types.SimpleNamespace(text='{"vote": "TAKE", "score": 80, "summary": "fine", "points": []}')
        desk.client = types.SimpleNamespace(aio=types.SimpleNamespace(models=Models()))
        v = asyncio.run(desk.review(setup, market, SESSION, history="last 3 trades: 2 losses"))
        analysts = [r for r in v["reports"] if r["stage"] == 1]
        changed = [r for r in analysts if r.get("changed")]
        self.assertEqual(len(changed), 6)  # the 2 most confident dissenters of each desk were challenged
        self.assertTrue(all(r["vote"] == "SKIP" for r in changed))
        self.assertFalse(v["approved"])
        flows = list(desk.monitor.flows)
        self.assertTrue(any(f["kind"] == "challenge" and f["from"] == "tech_lead" and f["to"] == "structure" for f in flows))
        self.assertTrue(any(f["kind"] == "reply" and f["from"] == "structure" for f in flows))
        self.assertTrue(any(f["from"] == "engine" and ("SELL" in f["text"] or "BUY" in f["text"]) for f in flows))
        self.assertTrue(all(r.get("debate") for r in changed))

    def test_multiple_keys_become_slots(self):
        from agents import TradingDesk, split_slot
        desk = TradingDesk(["k1", "k2"], "m-a", 1.5, 70, 5, ["m-b"])
        self.assertEqual(desk.pool.models, ["m-a#1", "m-a#2", "m-b#1", "m-b#2"])
        self.assertEqual(split_slot("m-b#2"), ("m-b", 1))
        self.assertEqual(len(desk.clients), 2)
        self.assertEqual(desk.pool.status()[1]["model"], "m-a · key 2")


class AgentPlanTests(unittest.TestCase):
    def test_plan_spreads_keys_and_tiers(self):
        from agents import ANALYSTS, VERIFIERS, TradingDesk
        desk = TradingDesk(["k1", "k2"], "big-a", 1.5, 70, 5, ["big-b", "big-c", "x-lite", "y-lite"])
        analyst_first = [desk.slots_for(a["key"])[0] for a in ANALYSTS]
        self.assertTrue(all("lite" in s for s in analyst_first))
        self.assertEqual(sum(s.endswith("#1") for s in analyst_first), 9)  # half the analysts on each key
        from agents import ALL_AGENTS
        homes = [desk.home_key(a["key"]) for a in ALL_AGENTS]
        self.assertEqual((homes.count(1), homes.count(2)), (13, 13))    # 26 agents split 13 / 13
        self.assertTrue(all("lite" not in desk.slots_for(v["key"])[0] for v in VERIFIERS))
        self.assertEqual(desk.slots_for("head")[0], "big-a#1")
        self.assertNotEqual(desk.slots_for("auditor")[0].split("#")[1], "1")

    def test_focused_briefs(self):
        from agents import market_brief
        _, market, _ = first_setup()
        vol = json.loads(market_brief(market, "volume"))
        self.assertEqual(set(vol["M15"]), {"price", "volume", "indicators"})
        self.assertEqual(set(vol["M15"]["indicators"]), {"obv"} & set(market["15min"]["ind"]) - {None} or set())
        liq = json.loads(market_brief(market, "liquidity"))
        self.assertIn("key_levels", liq)
        self.assertNotIn("indicators", liq["H1"])
        full = json.loads(market_brief(market))
        self.assertIn("indicators", full["H1"])

    def test_pool_learns_and_backs_off(self):
        from agents import ModelPool
        pool = ModelPool(["a", "b", "c"], rpm=10)
        pool.success("c", 1.0)
        pool.success("c", 1.0)
        pool.penalize("b", RuntimeError("503 UNAVAILABLE"))
        self.assertEqual(pool.order("a")[:2], ["a", "c"])  # preferred first, then the healthiest
        first = pool.cool_until["b"] - time.monotonic()
        pool.penalize("b", RuntimeError("503 UNAVAILABLE"))
        self.assertGreater(pool.cool_until["b"] - time.monotonic(), first * 1.5)  # growing back-off
        pool.penalize("a", RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel"))
        saved = pool.export()
        fresh = ModelPool(["a", "b", "c"])
        fresh.restore(saved)
        self.assertFalse(fresh.status()[0]["ready"])  # still resting after a restart


class GeminiErrorTests(unittest.TestCase):
    def test_classify_errors(self):
        from agents import classify_error
        day = RuntimeError("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        minute = RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerMinute... Please retry in 41.7s.")
        self.assertEqual(classify_error(day)[0], "quota_day")
        self.assertGreater(classify_error(day)[2], 60)
        kind, text, wait = classify_error(minute)
        self.assertEqual((kind, round(wait)), ("quota_min", 43))
        self.assertEqual(classify_error(RuntimeError("503 UNAVAILABLE high demand"))[0], "busy")
        self.assertEqual(classify_error(RuntimeError("404 NOT_FOUND no longer available"))[0], "missing")
        self.assertEqual(classify_error(RuntimeError("400 API key not valid"))[0], "key")

    def test_ping_uses_one_call_per_model_and_backup(self):
        desk = fake_desk()

        class Models:
            calls = 0

            async def generate_content(self, model, contents, config):
                Models.calls += 1
                if model == "m1":
                    raise RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel")
                return types.SimpleNamespace(text='{"ok": true}')
        desk.client = types.SimpleNamespace(aio=types.SimpleNamespace(models=Models()))
        results = asyncio.run(desk.ping())
        self.assertEqual(Models.calls, 2)  # 2 models, not 13 agents
        self.assertEqual({r["model"]: r["ok"] for r in results}, {"m1": False, "m2": True})
        self.assertTrue(all(a["status"] == "done" for a in desk.monitor.agents.values()))
        self.assertEqual(asyncio.run(desk.ping()), [])  # throttled
        self.assertFalse(desk.pool.status()[0]["ready"])
        self.assertIn("daily quota", desk.pool.status()[0]["reason"])


class ModelPoolTests(unittest.TestCase):
    def test_spreads_and_skips_rate_limited_models(self):
        from agents import ModelPool

        async def run():
            pool = ModelPool(["a", "b"], rpm=2)
            pool.max_wait = 0  # do not wait for resting slots in this test
            got = [await pool.acquire("a", set()) for _ in range(4)]
            pool.penalize("a", RuntimeError("429 RESOURCE_EXHAUSTED"))
            cooling = pool._ready_at("a", time.monotonic() - 50) > time.monotonic()
            return got, cooling, await pool.acquire("a", {"b"})  # "a" is cooling -> skipped, not awaited
        got, cooling, none_left = asyncio.run(run())
        self.assertEqual(got, ["a", "a", "b", "b"])
        self.assertTrue(cooling)
        self.assertIsNone(none_left)


class LevelsAndEngineTests(unittest.TestCase):
    def test_key_levels(self):
        from levels import key_levels
        daily = [candle("2026-09-14 00:00:00", 10, 20, 5, 15), candle("2026-09-18 00:00:00", 15, 30, 12, 20),
                 candle("2026-09-21 00:00:00", 20, 25, 18, 22), candle("2026-09-22 00:00:00", 22, 24, 21, 23)]
        m5 = [candle("2026-09-22 01:00:00", 22, 23.5, 21.5, 23), candle("2026-09-22 08:00:00", 23, 26, 20, 24)]
        lv = key_levels(daily, m5)
        self.assertEqual((lv["PDH"], lv["PDL"]), (25, 18))
        self.assertEqual((lv["PWH"], lv["PWL"]), (30, 5))
        self.assertEqual((lv["Asia High"], lv["Asia Low"]), (23.5, 21.5))

    def test_setups_respect_clear_path(self):
        for seed in range(80):
            market = analyze_market(random_market(seed))
            for style in setups.STYLES:
                s = find_setup(style, market, SESSION, 1.5)
                if not s:
                    continue
                st = setups.STYLES[style]
                opp = "bearish" if s["direction"] == "BUY" else "bullish"
                zones = [z for z in market[st["entry"]]["smc"]["order_blocks"] + market[st["confirm"]]["smc"]["order_blocks"]
                         if z["direction"] == opp]
                for z in zones:
                    edge = z["bottom"] if s["direction"] == "BUY" else z["top"]
                    between = s["entry"] < edge < s["tps"][0]["price"] if s["direction"] == "BUY" \
                        else s["tps"][0]["price"] < edge < s["entry"]
                    self.assertFalse(between, f"opposing zone at {edge} blocks TP1 in {s}")


class PatternTests(unittest.TestCase):
    def test_candle_patterns(self):
        from patterns import candle_patterns, daily_range, double_tops_bottoms
        engulf = [candle("1", 10, 10.5, 9, 9.2), candle("2", 9.2, 9.4, 8.8, 9.0), candle("3", 8.9, 10.8, 8.8, 10.6)]
        self.assertIn("Bullish Engulfing", [x["name"] for x in candle_patterns(engulf)])
        pin = [candle("1", 10, 10.5, 9, 9.2), candle("2", 9.2, 9.4, 8.8, 9.0), candle("3", 9.6, 9.75, 7.0, 9.7)]
        self.assertIn("Bullish Pin Bar (hammer)", [x["name"] for x in candle_patterns(pin)])
        swings = [{"type": "high", "price": 110.0}, {"type": "low", "price": 100}, {"type": "high", "price": 110.2}]
        self.assertEqual(double_tops_bottoms(swings, 0.5)[0]["name"], "Double Top")
        daily = [candle(str(i), 100, 110, 100, 105) for i in range(15)] + [candle("t", 100, 105, 100, 104)]
        self.assertEqual(daily_range(daily)["used_pct"], 50)


class StrictModeTests(unittest.TestCase):
    def test_strict_is_a_stricter_subset(self):
        normal = strict = 0
        for seed in range(80):
            market = analyze_market(random_market(seed))
            for style in setups.STYLES:
                a = find_setup(style, market, SESSION, 1.5)
                b = find_setup(style, market, SESSION, 1.5, strict=True)
                normal += a is not None
                strict += b is not None
                if b:
                    self.assertIsNotNone(a)
                    self.assertIn("Strict mode", " ".join(b["confluences"]))
        self.assertLess(strict, normal)

    def test_new_indicators_in_market_read(self):
        _, market, _ = first_setup()
        ind = market["15min"]["ind"]
        for key in ("adx", "supertrend", "stoch_rsi", "bollinger", "vwap"):
            self.assertIn(key, ind)
        self.assertIn(ind["supertrend"]["direction"], ("bullish", "bearish"))


class NewsTests(unittest.TestCase):
    def test_parse_blackout_and_alerts(self):
        from news import NewsCalendar, parse
        items = [{"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-02T08:30:00-04:00",
                  "impact": "High", "forecast": "150K", "previous": "142K"},
                 {"title": "ISM", "country": "USD", "date": "2026-10-02T10:00:00-04:00", "impact": "Medium"},
                 {"title": "German CPI", "country": "EUR", "date": "2026-10-02T08:00:00-04:00", "impact": "High"},
                 {"title": "Bank Holiday", "country": "USD", "date": "2026-10-02T00:00:00-04:00", "impact": "Holiday"}]
        events = parse(items, ("USD",))
        self.assertEqual([e["title"] for e in events], ["Non-Farm Employment Change", "ISM"])
        self.assertEqual(events[0]["time"], datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc))
        cal = NewsCalendar()
        cal.events = events
        self.assertIsNotNone(cal.blackout(datetime(2026, 10, 2, 12, 10, tzinfo=timezone.utc)))
        self.assertIsNone(cal.blackout(datetime(2026, 10, 2, 13, 10, tzinfo=timezone.utc)))
        self.assertIsNone(cal.blackout(datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)))  # medium impact only
        at = datetime(2026, 10, 2, 12, 15, tzinfo=timezone.utc)
        self.assertEqual(len(cal.due_alerts(at)), 1)
        self.assertEqual(cal.due_alerts(at), [])  # only once
        self.assertIn("Non-Farm", " ".join(e["title"] for e in cal.upcoming(at, 48)))


class HeadlineTests(unittest.TestCase):
    def test_parse_rss_filters_gold_news(self):
        from news import parse_rss
        xml = """<rss><channel>
          <item><title>Gold climbs as Fed rate cut bets grow</title><pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate>
                <link>https://x/1</link></item>
          <item><title>Japanese stocks close flat</title><pubDate>Mon, 21 Sep 2026 09:00:00 GMT</pubDate></item>
          <item><title>US CPI beats expectations, dollar jumps</title><pubDate>bad date</pubDate></item>
        </channel></rss>"""
        items = parse_rss(xml, "test")
        self.assertEqual([i["title"][:4] for i in items], ["Gold", "US C"])
        self.assertTrue(items[0]["gold"])
        self.assertEqual(items[0]["time"], datetime(2026, 9, 21, 10, tzinfo=timezone.utc))
        self.assertIsNone(items[1]["time"])


class LotSizeTests(unittest.TestCase):
    def test_lot_size(self):
        self.assertEqual(labels.lot_size(1000, 1, 5.0)[0], 0.02)   # $10 risk / ($5 * 100oz)
        self.assertEqual(labels.lot_size(10000, 2, 2.5)[0], 0.8)
        self.assertEqual(labels.lot_size(1000, 1, 0)[0], 0.0)
        self.assertEqual(labels.plain("\U0001F7E2 Scalp  (M5)"), "Scalp (M5)")


def synthetic_history(days=12, seed=1):
    rnd, price, t, m5, drift = random.Random(seed), 2650.0, datetime(2026, 8, 3), [], 0.0
    while t < datetime(2026, 8, 3) + timedelta(days=days):
        if t.weekday() < 5:
            if rnd.random() < 0.01:
                drift = rnd.choice([-1, 1]) * rnd.uniform(0.02, 0.15)
            o = price
            c = o + drift + rnd.gauss(0, 0.9)
            m5.append(candle(t.strftime("%Y-%m-%d %H:%M:%S"), o, max(o, c) + abs(rnd.gauss(0, .5)),
                             min(o, c) - abs(rnd.gauss(0, .5)), c))
            price = c
        t += timedelta(minutes=5)

    def agg(minutes):
        out = {}
        for c in m5:
            dt = datetime.strptime(c["time"], "%Y-%m-%d %H:%M:%S")
            k = dt.replace(hour=0, minute=0) if minutes == 1440 else dt - timedelta(minutes=(dt.hour * 60 + dt.minute) % minutes)
            key = k.strftime("%Y-%m-%d %H:%M:%S")
            if key not in out:
                out[key] = dict(c, time=key)
            else:
                b = out[key]
                b["high"], b["low"], b["close"] = max(b["high"], c["high"]), min(b["low"], c["low"]), c["close"]
        return list(out.values())
    return {"5min": m5, "15min": agg(15), "1h": agg(60), "4h": agg(240), "1day": agg(1440)}


class BacktestTests(unittest.TestCase):
    def test_runs_without_lookahead_and_reports(self):
        import backtest
        data = synthetic_history(days=30)
        r = backtest.run(data, "intraday")
        self.assertNotIn("error", r)
        self.assertGreater(r["steps"], 100)
        for t in r["trades"]:
            # every signal was created from candles that had already closed
            self.assertLessEqual(t["created_candle"], t["created_at"][:19].replace("T", " "))
        self.assertIn("stats", r)
        self.assertIn("error", backtest.run({k: v[:50] for k, v in data.items()}, "swing"))

    def test_realized_r(self):
        t, _ = make_trade(entry_type="MARKET")
        t["stage"] = 1
        self.assertAlmostEqual(tracker.realized_r(t, t["entry"]), 0.5)   # 1/3 * 1.5R, rest at breakeven
        t["stage"] = 3
        self.assertAlmostEqual(tracker.realized_r(t), (1.5 + 2.5 + 4) / 3)


def make_bot(data, **env):
    """A website-only app on a temp data file with fake market data, news and AI desk."""
    import bot as botmod
    from config import load_config
    tmp = tempfile.TemporaryDirectory()
    env = {"GEMINI_API_KEY": "x", "TWELVEDATA_API_KEY": "x", "DATA_FILE": os.path.join(tmp.name, "data.json"), **env}
    keep = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        gb = botmod.GoldBot(load_config())
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    gb.desk = fake_desk()
    gb._tmp = tmp

    async def get():
        return data, {}
    gb.data.get = get

    async def no_news():
        return None
    gb.news.refresh = no_news
    gb.headlines.refresh = no_news
    return gb


EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")


class EndToEndTests(unittest.TestCase):
    def test_scan_publishes_and_tracks_on_the_website(self):
        setup, market, data = first_setup()
        gb = make_bot(data, STYLES=setup["style"], MIN_ENGINE_SCORE="0")
        self.addCleanup(gb._tmp.cleanup)
        gb.apply_setting({"account": {"balance": 5000, "risk": 1}})

        notes = asyncio.run(gb.scan(manual=True))
        self.assertIn("signal published", " ".join(notes))
        trade = gb.storage.open_trades()[0]
        feed = list(gb.desk.monitor.signal_feed)
        self.assertEqual(feed[0]["kind"], "new")
        self.assertIn("XAU/USD", feed[0]["text"])
        st = gb.dashboard_state()
        card = st["signals"]["open"][0]
        self.assertEqual(card["id"], trade["id"])
        self.assertGreaterEqual(card["lot"]["lots"], 0.01)
        self.assertIn(card["smc_grade"], ("A+", "A", "B", "C"))
        self.assertIn("conviction", card)
        self.assertIsNone(EMOJI.search(json.dumps(st, ensure_ascii=False)))
        page = open(os.path.join(os.path.dirname(__file__), "..", "static", "dashboard.html"), encoding="utf-8").read()
        self.assertIsNone(EMOJI.search(page))

        # Same setup again is not re-published.
        asyncio.run(gb.scan(manual=True))
        self.assertEqual(len(gb.storage.open_trades()), 1)

        # Price hits the stop on a new candle -> update in the website feed, trade closed.
        bull = trade["direction"] == "BUY"
        level = trade["stop_loss"] - 1 if bull else trade["stop_loss"] + 1
        data["5min"].append(candle("9999-12-31 23:59:00", level, max(level, trade["entry"]),
                                   min(level, trade["entry"]), level))
        asyncio.run(gb.scan(manual=True))
        self.assertEqual(gb.storage.trade(trade["id"])["status"], "closed")
        kinds = [f["kind"] for f in gb.desk.monitor.signal_feed]
        self.assertTrue({"sl", "expired", "cancelled", "filled"} & set(kinds), kinds)
        self.assertTrue(gb.dashboard_state()["signals"]["closed"])

        # A high-impact event right now pauses new signals and warns on the website.
        gb.news.events = [{"title": "CPI m/m", "country": "USD", "impact": "High", "forecast": "", "previous": "",
                           "time": datetime.now(timezone.utc) + timedelta(minutes=10)}]
        notes = asyncio.run(gb.scan(manual=True))
        self.assertIn("News pause", notes[0])
        self.assertTrue(any(f["kind"] == "news" for f in gb.desk.monitor.signal_feed))

    def test_event_text_for_every_update(self):
        import bot as botmod
        t, _ = make_trade()
        for ev in ({"kind": "filled", "price": 100}, {"kind": "tp", "n": 1, "price": 103, "rr": 1.5, "new_sl": 100},
                   {"kind": "tp", "n": 3, "price": 108, "rr": 4}, {"kind": "sl", "price": 98},
                   {"kind": "protected_stop", "price": 100, "stage": 1}, {"kind": "expired"},
                   {"kind": "cancelled"}, {"kind": "timeout", "price": 101}):
            text = botmod.event_text(t, ev)
            self.assertTrue(text.startswith("XAU/USD BUY Intraday"), text)
            self.assertIsNone(EMOJI.search(text))
        self.assertIn("limit", botmod.failure_hint("You have run out of API credits"))


class WebsiteControlTests(unittest.TestCase):
    """Settings, lab, practice review, health alerts and market-closed refresh - all driven by the website."""

    def setUp(self):
        self.setup, market, self.data = first_setup()
        self.gb = make_bot(self.data)
        self.addCleanup(self.gb._tmp.cleanup)

    def test_settings_account_and_style_switches(self):
        gb = self.gb
        self.assertEqual(gb.apply_setting({"account": {"balance": "2500", "risk": "2"}}), {"ok": True})
        self.assertEqual(gb.storage.account, {"balance": 2500.0, "risk": 2.0})
        gb.apply_setting({"account": {"balance": "", "risk": 50}})
        self.assertEqual(gb.storage.account, {"balance": None, "risk": 10.0})
        self.assertFalse(gb.apply_setting({"style": "nope", "action": "on"})["ok"])
        self.assertFalse(gb.apply_setting({"style": "scalp", "action": "apply", "index": 0})["ok"])
        gb.apply_setting({"style": "scalp", "action": "off"})
        self.assertFalse(gb.style_params("scalp")["enabled"])
        notes = asyncio.run(gb.scan(manual=True))
        self.assertTrue(any("switched off" in n for n in notes))
        gb.apply_setting({"style": "scalp", "action": "reset"})
        self.assertTrue(gb.style_params("scalp")["enabled"])
        st = gb.dashboard_state()
        self.assertEqual(st["account"]["risk"], 10.0)
        self.assertIn("scalp", st["styles"])

    def test_lab_backtest_optimize_and_apply(self):
        import backtest
        hist = synthetic_history(days=30)

        async def fake_fetch(*a):
            return hist
        orig = backtest.fetch_history
        backtest.fetch_history = fake_fetch
        try:
            asyncio.run(self.gb.run_lab("backtest", "intraday"))
            self.assertFalse(self.gb.lab["running"])
            self.assertIn("stats", self.gb.lab["result"])
            self.assertNotIn("trades", self.gb.lab["result"])
            asyncio.run(self.gb.run_lab("optimize", "intraday"))
        finally:
            backtest.fetch_history = orig
        ranked = self.gb.last_optimize["intraday"]["ranked"]
        if ranked:
            self.assertTrue(self.gb.apply_setting({"style": "intraday", "action": "apply", "index": 0})["ok"])
            self.assertEqual(self.gb.style_params("intraday")["min_score"], ranked[0]["config"]["min_score"])
        self.assertIn("lab", self.gb.dashboard_state())

    def test_practice_review_runs_desk_without_publishing(self):
        verdict = asyncio.run(self.gb.practice_review())
        self.assertIsNotNone(verdict)
        self.assertEqual(self.gb.storage.open_trades(), [])
        self.assertTrue(self.gb.desk.monitor.reviews[0]["practice"])

    def test_market_closed_still_refreshes_data(self):
        import instruments as ins
        orig = ins.is_open
        ins.is_open = lambda inst, now=None: False
        try:
            notes = asyncio.run(self.gb.scan())
        finally:
            ins.is_open = orig
        self.assertEqual(notes, ["Market closed"])
        self.assertIsNotNone(self.gb.market)
        self.assertIn("refreshed", self.gb.desk.monitor.log[-1]["text"])

    def test_health_alerts_on_the_website(self):
        good_get = self.gb.data.get

        async def broken():
            raise RuntimeError("Twelve Data (5min): You have run out of API credits for the current minute.")
        self.gb.data.get = broken
        import instruments as ins
        orig = ins.is_open
        ins.is_open = lambda inst, now=None: True
        try:
            for _ in range(3):
                asyncio.run(self.gb.scheduled_scan())
            alerts = [f["text"] for f in self.gb.desk.monitor.signal_feed if f["kind"] == "alert"]
            self.assertTrue(any("limit" in a for a in alerts), alerts)
            self.gb.data.get = good_get
            asyncio.run(self.gb.scheduled_scan())
            self.assertTrue(any("Recovered" in f["text"] for f in self.gb.desk.monitor.signal_feed))
            self.assertEqual(self.gb.fail_count, 0)
        finally:
            ins.is_open = orig


class DashboardTests(unittest.TestCase):
    def test_serves_page_state_candles_and_settings(self):
        import urllib.request
        from dashboard import Dashboard

        setup, market, data = first_setup()
        gb = make_bot(data)
        self.addCleanup(gb._tmp.cleanup)

        async def run():
            dash = Dashboard(gb, asyncio.get_running_loop(), "127.0.0.1", 0)
            dash.start()
            port = dash.server.server_address[1]
            base = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            fetch = lambda path: opener.open(base + path).read()  # noqa: E731

            def post(path, body):
                req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
                return json.loads(opener.open(req).read())
            page = await asyncio.to_thread(fetch, "/")
            before = json.loads(await asyncio.to_thread(fetch, "/api/state"))
            gb.candles, gb.market = data, market
            gb.last_scan = datetime.now(timezone.utc)
            candles = json.loads(await asyncio.to_thread(fetch, "/api/candles?tf=15min"))
            js = await asyncio.to_thread(fetch, "/static/lightweight-charts.js")
            saved = await asyncio.to_thread(post, "/api/settings", {"account": {"balance": 1000, "risk": 1}})
            bad = await asyncio.to_thread(post, "/api/settings", {"style": "x", "action": "on"})
            after = json.loads(await asyncio.to_thread(fetch, "/api/state"))
            dash.server.shutdown()
            return page, before, candles, js, saved, bad, after
        page, before, candles, js, saved, bad, after = asyncio.run(run())
        self.assertEqual(len(candles["candles"]), 300)
        self.assertIn(b"Lightweight Charts", js[:300])
        self.assertIn(b"AI Control Room", page)
        self.assertIn(b"Agent network", page)
        self.assertNotIn(b"Telegram", page)
        self.assertIn("current", before)
        self.assertTrue(all(a["slot"] for a in before["agents"]))
        self.assertEqual(len(before["agents"]), 26)
        self.assertIsNone(before["market"])
        self.assertEqual(saved, {"ok": True})
        self.assertFalse(bad["ok"])
        self.assertEqual(after["account"]["balance"], 1000)
        self.assertEqual(len(after["market"]["tfs"]), 5)


class MultiMarketTests(unittest.TestCase):
    """Gold closed (weekend) while bitcoin trades: BTC is scanned, signalled and shown separately."""

    def setUp(self):
        setup, market, data = first_setup()
        self.gb = make_bot(data, MARKETS="XAUUSD,BTCUSD")
        self.addCleanup(self.gb._tmp.cleanup)
        btc = {tf: [dict(c, open=c["open"] * 25, high=c["high"] * 25, low=c["low"] * 25, close=c["close"] * 25,
                         volume=100 + (i % 7) * 30) for i, c in enumerate(cs)] for tf, cs in data.items()}
        self.style = setup["style"]

        async def gold():
            return data, {}

        async def bitcoin():
            return btc, btc
        self.gb.feeds["XAUUSD"].get = gold
        self.gb.feeds["BTCUSD"].get = bitcoin

    def test_btc_scanned_while_gold_closed(self):
        import instruments as ins
        orig = ins.is_open
        ins.is_open = lambda inst, now=None: inst["key"] == "BTCUSD"
        try:
            notes = asyncio.run(self.gb.scan())
        finally:
            ins.is_open = orig
        self.assertTrue(any("BTC/USD" in n for n in notes))
        self.assertFalse(any("XAU/USD" in n and "signal" in n for n in notes))
        trades = self.gb.storage.open_trades()
        self.assertTrue(trades and all(t["instrument"] == "BTCUSD" and t["pip"] == 1.0 for t in trades))
        self.assertTrue(any("BTC/USD" in f["text"] for f in self.gb.desk.monitor.signal_feed))
        self.assertIn("XAUUSD", self.gb.markets)  # closed gold still refreshed for the charts

        st = self.gb.dashboard_state()
        self.assertEqual({i["key"] for i in st["instruments"]}, {"XAUUSD", "BTCUSD"})
        self.assertIsNotNone(st["markets"]["BTCUSD"])
        self.assertEqual(st["signals"]["open"][0]["symbol"], "BTC/USD")
        cd = self.gb.chart_data("15min", "BTCUSD")
        self.assertEqual(cd["symbol"], "BTC/USD")
        self.assertTrue(cd["market_open"])
        self.assertEqual(len({(m["time"], m["text"], m["position"]) for m in cd["markers"]}), len(cd["markers"]))

    def test_drop_closed_and_binance_pagination(self):
        import backtest
        from instruments import drop_closed
        weekend = [candle("2026-09-26 03:00:00", 1, 1, 1, 1), candle("2026-09-25 20:55:00", 1, 2, 0, 1)]
        self.assertEqual([c["time"] for c in drop_closed(weekend, "5min")], ["2026-09-25 20:55:00"])

        pages = []

        class Client:
            async def get(self, url, params, timeout):
                end = params.get("endTime", 1_790_000_000_000)
                pages.append(end)
                rows = [[t * 300_000, "1", "2", "0.5", "1.5", "10"] for t in range(max(0, end // 300_000 - 999),
                                                                                    end // 300_000 + 1)]
                return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: rows)
        out = asyncio.run(backtest.fetch_binance_history(Client(), "BTCUSDT", "5min", total=1500))
        self.assertEqual(len(out), 1500)
        self.assertEqual(len(pages), 2)
        self.assertTrue(all(a["time"] < b["time"] for a, b in zip(out, out[1:])))


class StrategyAndIndicatorTests(unittest.TestCase):
    def test_new_indicators(self):
        import indicators as ind
        up = [{"time": f"2026-09-{1 + i // 24:02d} {i % 24:02d}:00:00", "open": 100 + i, "high": 101.5 + i,
               "low": 99.5 + i, "close": 101 + i} for i in range(120)]
        self.assertEqual(ind.ichimoku(up)["position"], "above cloud")
        self.assertEqual(ind.heikin_ashi_trend(up), "bullish")
        self.assertGreater(ind.stochastic(up)["k"], 80)
        self.assertGreater(ind.williams_r(up), -20)
        self.assertEqual(ind.donchian(up)["breakout"], "up")
        piv = ind.pivots([{"high": 110, "low": 90, "close": 100}, up[-1]])
        self.assertEqual((piv["P"], piv["R1"], piv["S1"]), (100, 110, 90))
        self.assertEqual(ind.regime({"adx": {"adx": 30, "plus_di": 25, "minus_di": 10}})["regime"], "trending")
        self.assertEqual(ind.regime({"adx": {"adx": 15, "plus_di": 12, "minus_di": 10}, "bb_width_rank": 50})["regime"],
                         "ranging")
        self.assertEqual(ind.regime({"adx": {"adx": 22, "plus_di": 12, "minus_di": 10}, "squeeze": {"on": True}})["regime"],
                         "compressed")
        fib = ind.fib_levels(200, 100)
        self.assertAlmostEqual(fib["0.705"], 129.5)

    def test_strategy_board(self):
        import strategies
        setup, market, _ = first_setup()
        board = strategies.evaluate(market, setup["direction"], setup["timeframes"], setup["entry"])
        self.assertGreaterEqual(len(board["results"]), 12)
        self.assertEqual(board["agrees"] + board["against"] + board["neutral"] + board["ignored"], len(board["results"]))
        self.assertIn(board["regime"], ("trending", "ranging", "compressed", "volatile", "transition"))
        # strategies outside their regime are shown but not counted
        self.assertEqual(board["ignored"], sum(not x["applicable"] for x in board["results"]))
        self.assertTrue({"trend", "breakout", "reversion", "smc", "momentum"} <= set(board["groups"]))
        flipped = strategies.evaluate(market, "SELL" if setup["direction"] == "BUY" else "BUY",
                                      setup["timeframes"], setup["entry"])
        self.assertLessEqual(flipped["agrees"], board["agrees"] + board["against"])
        self.assertTrue(all(" - " in line for line in strategies.brief(board, ("trend",))))

    def test_headline_categories(self):
        from news import categorize
        self.assertEqual(categorize("Iran missile strikes lift gold"), ["gold", "world"])
        self.assertIn("macro", categorize("Powell signals rate cuts as CPI cools"))
        self.assertIn("crypto", categorize("Bitcoin ETF flows hit record"))

    def test_desk_gets_its_own_data(self):
        import agents
        setup, market, _ = first_setup()
        ctx = {"_market": market, "_board": {}, "instrument": "XAU/USD (gold)", "_expiry": 60,
               "_extra": {"calendar": ["Thu 12:30 UTC High USD: CPI"],
                          "headlines": {"world": ["10:00 UTC bbc: Oil jumps on Gulf tensions"]}}}
        spec = {a["key"]: a for a in agents.ANALYSTS}
        cal = json.loads(agents.agent_data(spec["calendar"], ctx))
        self.assertIn("CPI", str(cal["economic_calendar_next_hours"]))
        world = json.loads(agents.agent_data(spec["world"], ctx))
        self.assertIn("Gulf", str(world["world_headlines_last_24h"]))
        self.assertNotIn("M15", world)  # a news agent gets no chart data
        mom = json.loads(agents.agent_data(spec["momentum"], ctx))
        self.assertTrue(set(mom["M15"]["indicators"]) <= set(spec["momentum"]["ind"]))
        self.assertEqual(len(agents.ALL_AGENTS), 26)
        link = agents.links()
        self.assertEqual(link["world"]["to"], ["macro_lead"])
        self.assertIn("head", link["tech_lead"]["to"])


class AgentRecordTests(unittest.TestCase):
    def test_records_and_weighted_agreement(self):
        from agents import agent_records, records_text, ANALYSTS
        rep = lambda k, v: {"key": k, "vote": v}  # noqa: E731
        trades = [{"outcome": "win", "stage": 2, "reports": [rep("structure", "TAKE"), rep("volume", "SKIP")]},
                  {"outcome": "loss", "stage": 0, "reports": [rep("structure", "SKIP"), rep("volume", "TAKE")]},
                  {"outcome": "win", "stage": 1, "reports": [rep("structure", "TAKE"), rep("volume", "SKIP")]},
                  {"outcome": "expired", "stage": 0, "reports": [rep("structure", "TAKE")]}]
        rec = agent_records(trades)
        self.assertEqual((rec["structure"]["n"], rec["structure"]["accuracy"]), (3, 100))
        self.assertEqual(rec["volume"]["accuracy"], 0)
        self.assertGreater(rec["structure"]["weight"], 1.0)
        self.assertLess(rec["volume"]["weight"], 1.0)
        self.assertIn("Market Structure Analyst 100%", records_text(rec, ANALYSTS))
        # the desk refuses when only the historically wrong analysts agree
        setup, market, _ = first_setup()
        desk = fake_desk()
        desk.records = {a["key"]: {"weight": 1.5 if i < 9 else 0.5} for i, a in enumerate(ANALYSTS)}
        v = asyncio.run(desk.review(setup, market, SESSION))
        self.assertEqual(v["weighted_agreement"], 100)


class QuotaHandlingTests(unittest.TestCase):
    def test_pool_waits_for_a_short_rest_but_not_for_daily_quota(self):
        from agents import ModelPool

        async def run():
            pool = ModelPool(["a"], rpm=10)
            pool.max_wait = 2
            pool.cool_until["a"] = time.monotonic() + 0.6  # e.g. "busy" back-off ending soon
            started = time.monotonic()
            got = await pool.acquire("a", set())
            waited = time.monotonic() - started
            pool.penalize("a", RuntimeError("429 RESOURCE_EXHAUSTED quota PerDay"))
            return got, waited, await pool.acquire("a", set())
        got, waited, none = asyncio.run(run())
        self.assertEqual(got, "a")
        self.assertGreater(waited, 0.4)
        self.assertIsNone(none)

    def test_review_stops_cleanly_when_every_key_is_out_of_quota(self):
        setup, market, _ = first_setup()
        desk = fake_desk()
        for m in desk.pool.models:
            desk.pool.penalize(m, RuntimeError("429 RESOURCE_EXHAUSTED quota PerDay"))
        v = asyncio.run(desk.review(setup, market, SESSION))
        self.assertFalse(v["approved"])
        self.assertTrue(v["ai_down"])
        self.assertEqual(desk.fake.calls, 0)  # no doomed requests were sent
        self.assertEqual(desk.monitor.agents["head"]["status"], "error")
        self.assertIn("quota", desk.monitor.agents["tech_lead"]["summary"])

    def test_unchanged_analyst_input_is_reused(self):
        setup, market, _ = first_setup()
        desk = fake_desk()
        asyncio.run(desk.review(setup, market, SESSION))
        first = desk.fake.calls
        asyncio.run(desk.review(setup, market, SESSION))
        self.assertEqual(desk.fake.calls - first, first - 18)  # all 18 analysts reused, the rest asked again


class ProviderTests(unittest.TestCase):
    def make(self):
        from agents import TradingDesk
        prov = [{"name": "Mistral", "base": "https://api.mistral.test/v1", "key": "mk",
                 "models": ["mistral-large-latest", "mistral-medium-latest", "mistral-small-latest"], "interval": 0}]
        return TradingDesk(["gk"], "gem-a", 1.5, 70, 5, ["gem-lite"], providers=prov)

    def test_mistral_goes_first_and_gemini_is_fallback(self):
        from agents import ANALYSTS
        desk = self.make()
        self.assertIn("mistral-small-latest#2", desk.pool.models)
        self.assertIn("gem-a#1", desk.pool.models)
        firsts = {desk.slots_for(a["key"])[0] for a in ANALYSTS}
        self.assertEqual(firsts, {"mistral-small-latest#2", "mistral-medium-latest#2"})
        self.assertEqual(desk.slots_for("head")[0], "mistral-large-latest#2")
        self.assertTrue(any(s.endswith("#1") for s in desk.slots_for("head")))  # Gemini as fallback
        self.assertEqual(desk.key_names(), ["Gemini 1", "Mistral 1"])
        self.assertEqual(desk.label("mistral-large-latest#2"), "mistral-large-latest · Mistral key 1")
        self.assertEqual(desk.home_key("structure"), 2)

    def test_openai_compatible_call_and_errors(self):
        import agents
        desk = self.make()
        seen = []

        class Resp:
            def __init__(self, code, data=None, text="", headers=None):
                self.status_code, self._d, self.text, self.headers = code, data, text, headers or {}

            def json(self):
                return self._d

        replies = [Resp(200, {"choices": [{"message": {"content": '{"vote": "TAKE", "score": 77}'}}]}),
                   Resp(429, text='{"message":"Requests rate limit exceeded"}'),
                   Resp(401, text="Unauthorized")]

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json, headers):
                seen.append((url, json, headers))
                return replies.pop(0)

        orig = agents.httpx.AsyncClient
        agents.httpx.AsyncClient = Client
        try:
            text = asyncio.run(desk._generate("mistral-large-latest#2", "hi"))
            with self.assertRaises(RuntimeError) as busy:
                asyncio.run(desk._generate("mistral-large-latest#2", "hi"))
            with self.assertRaises(RuntimeError) as bad:
                asyncio.run(desk._generate("mistral-large-latest#2", "hi"))
        finally:
            agents.httpx.AsyncClient = orig
        self.assertEqual(parse_json(text)["score"], 77)
        url, body, headers = seen[0]
        self.assertEqual(url, "https://api.mistral.test/v1/chat/completions")
        self.assertEqual((body["model"], body["response_format"]["type"]), ("mistral-large-latest", "json_object"))
        self.assertEqual(headers["Authorization"], "Bearer mk")
        kind, _, wait = agents.classify_error(busy.exception)
        self.assertEqual((kind, wait), ("quota_min", 6.0))  # short rest, not a minute
        self.assertEqual(agents.classify_error(bad.exception)[0], "key")

    def test_config_reads_provider_keys(self):
        import config
        os.environ["MISTRAL_API_KEY"] = "abc"
        try:
            provs = config._providers()
        finally:
            os.environ.pop("MISTRAL_API_KEY")
        self.assertEqual([(p["name"], p["key"]) for p in provs], [("Mistral", "abc")])
        self.assertIn("mistral-large-latest", provs[0]["models"])


class NoGeminiTests(unittest.TestCase):
    def test_desk_runs_on_mistral_only(self):
        from agents import ALL_AGENTS, TradingDesk
        prov = [{"name": "Mistral", "base": "https://m.test/v1", "key": "mk",
                 "models": ["mistral-large-latest", "mistral-small-latest"], "interval": 0}]
        desk = TradingDesk([], "gem-a", 1.5, 70, 5, ["gem-lite"], providers=prov)
        self.assertEqual(desk.pool.models, ["mistral-large-latest#1", "mistral-small-latest#1"])
        self.assertTrue(all(desk.slots_for(a["key"]) for a in ALL_AGENTS))
        self.assertTrue(all("gem" not in x for a in ALL_AGENTS for x in desk.slots_for(a["key"])))
        self.assertEqual(desk.key_names(), ["Mistral 1"])

    def test_config_without_gemini(self):
        import config
        env = {"TWELVEDATA_API_KEY": "x", "MISTRAL_API_KEY": "mk", "GEMINI_API_KEY": ""}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            cfg = config.load_config()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(cfg.gemini_api_keys, [])
        self.assertEqual(cfg.ai_providers[0]["name"], "Mistral")


class NeutralVoteTests(unittest.TestCase):
    def test_neutral_analysts_do_not_block_but_skips_do(self):
        setup, market, _ = first_setup()

        def run(neutral_every, skip_every=0):
            desk = fake_desk()
            desk.min_votes = 5  # the real default: 5/8 = 62.5 % of the analysts that take a side
            n = {"i": 0}

            class Models:
                async def generate_content(self, model, contents, config):
                    if "Signal Auditor" in contents:
                        return types.SimpleNamespace(text='{"approve": true, "issues": [], "note": "ok"}')
                    if "Head Trader" in contents:
                        return types.SimpleNamespace(text='{"decision": "TAKE", "confidence": 80, "reason": "ok"}')
                    if "desk lead challenges you" in contents:  # a challenged analyst keeps its vote
                        keep = "SKIP" if contents.split("Your first report was:")[1].strip().startswith("SKIP") else "NEUTRAL"
                        return types.SimpleNamespace(text=f'{{"vote": "{keep}", "score": 35, "changed": false, '
                                                          '"reply": "my data says so"}')
                    if "YOUR ONLY JOB" in contents:
                        n["i"] += 1
                        if skip_every and n["i"] % skip_every == 0:
                            return types.SimpleNamespace(text='{"vote": "SKIP", "score": 30, "summary": "against the trade", '
                                                                        '"evidence": ["CHoCH @ 2610", "RSI 71"]}')
                        if neutral_every and n["i"] % neutral_every == 0:
                            return types.SimpleNamespace(text='{"vote": "NEUTRAL", "score": 50, "summary": "quiet market now", '
                                                                        '"evidence": ["ADX 14", "range 2600-2620"]}')
                    return types.SimpleNamespace(text='{"vote": "TAKE", "score": 75, "summary": "fine setup here", '
                                                      '"evidence": ["BOS @ 2617.9", "OB 2612-2614"]}')
            desk.client = types.SimpleNamespace(aio=types.SimpleNamespace(models=Models()))
            return asyncio.run(desk.review(setup, market, SESSION)), desk
        v, desk = run(neutral_every=2)            # 9 TAKE, 9 NEUTRAL, 0 SKIP
        self.assertEqual(v["neutral"], 9)
        self.assertTrue(v["approved"], v.get("reject_reason"))
        v, desk = run(neutral_every=0, skip_every=2)   # 9 TAKE, 9 SKIP -> half against
        self.assertFalse(v["approved"])
        self.assertIn("analysts split", v["reject_reason"])
        self.assertEqual(desk.monitor.agents["auditor"]["status"], "skipped")
        v, _ = run(neutral_every=1)               # everyone neutral -> no edge, no trade
        self.assertFalse(v["approved"])
        self.assertIn("too few analysts see an edge", v["reject_reason"])
        self.assertGreaterEqual(v["conviction"], 0)

    def test_conviction_and_vetoes(self):
        from agents import TradingDesk
        desk = fake_desk()
        r = lambda k, v, sc: {"key": k, "vote": v, "score": sc}  # noqa: E731
        self.assertEqual(TradingDesk.support(r("x", "SKIP", 80)), 20)   # 'confident skip' = strongly against
        self.assertEqual(TradingDesk.support(r("x", "TAKE", 30)), 70)
        self.assertEqual(TradingDesk.support(r("x", "NEUTRAL", 90)), 60)
        analysts = [r(f"a{i}", "TAKE", 75) for i in range(12)] + [r(f"b{i}", "NEUTRAL", 50) for i in range(6)]
        board = {"agrees": 8, "against": 2, "score": 80}
        conv = desk._conviction(analysts, [r("l", "TAKE", 70)] * 3,
                                [r("confluence", "TAKE", 70), r("risk", "TAKE", 70), r("devil", "SKIP", 35)], board, {})
        self.assertTrue(60 <= conv <= 75, conv)
        self.assertEqual(desk._vetoes(analysts, [r("risk", "SKIP", 10)], board)[0][:20], "Risk Manager veto: ")
        self.assertEqual(desk._vetoes(analysts, [r("risk", "SKIP", 40)], board), [])  # a mild concern is no veto


class StrictAgentTests(unittest.TestCase):
    def test_incomplete_answer_is_asked_again(self):
        agent = {"desk": "tech", "name": "x"}
        self.assertEqual(TradingDesk._incomplete({"vote": "TAKE", "score": 70, "summary": "clean BOS and sweep",
                                                  "evidence": ["BOS @ 2617.9", "sweep @ 2613"]}, 1, agent), [])
        lazy = TradingDesk._incomplete({"vote": "maybe", "summary": "ok", "evidence": ["looks good"]}, 1, agent)
        self.assertEqual(len(lazy), 4)
        self.assertIn("the evidence quotes no exact number from the data",
                      TradingDesk._incomplete({"vote": "SKIP", "score": 20, "summary": "no confirmation here",
                                               "evidence": ["weak", "flat"]}, 1, agent))
        self.assertEqual(TradingDesk._incomplete({"vote": "NEUTRAL", "score": 50, "summary": "mixed signals now",
                                                  "points": ["x"]}, 2, agent), ["the vote is missing or invalid"])

        desk = fake_desk()
        answers = iter(['{"vote": "TAKE", "score": 70, "summary": "fine"}',
                        '{"vote": "TAKE", "score": 72, "summary": "BOS confirmed on H1", '
                        '"evidence": ["BOS @ 2617.9", "OB 2612-2614"]}'])
        prompts = []

        async def gen(model, contents, config):
            prompts.append(contents)
            return types.SimpleNamespace(text=next(answers))
        desk.fake.generate_content = gen
        from agents import ANALYSTS
        r = asyncio.run(desk._call(ANALYSTS[0], "analyse the setup", desk.monitor, 1))
        self.assertTrue(r["retried"])
        self.assertEqual(r["score"], 72)
        self.assertIn("YOUR PREVIOUS ANSWER WAS REJECTED BECAUSE", prompts[1])


class SmcGradeTests(unittest.TestCase):
    def test_setup_has_grade_and_checklist(self):
        setup, market, _ = first_setup()
        self.assertIn(setup["smc_grade"], ("A+", "A", "B", "C"))
        self.assertEqual(len(setup["smc_checklist"]), 10)
        self.assertTrue(setup["smc_checklist"]["HTF bias"])
        v = asyncio.run(fake_desk().review(setup, market, SESSION))
        self.assertIn("conviction", v)
        t = tracker.new_trade(dict(setup, symbol_name="XAU/USD"), v, "2026-09-22 08:00:00")
        self.assertEqual(t["smc_grade"], setup["smc_grade"])
        self.assertEqual(len(t["smc_checklist"]), 10)
        self.assertEqual(t["conviction"], v["conviction"])


if __name__ == "__main__":
    unittest.main()
