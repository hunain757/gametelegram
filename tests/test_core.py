import asyncio
import json
import math
import os
import random
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone

import setups
import smc
import tracker
import ui
from agents import TradingDesk, parse_json
from indicators import atr, ema, macd, rsi
from sessions import is_market_open
from setups import analyze_market, find_setup, pick_targets, validate_levels
from storage import Storage, stats

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
    data = {}
    for k, (tf, vol) in enumerate(vols):
        rnd, price, out = random.Random(seed * 7 + k), 2650.0, []
        drift = 0.0
        for i in range(300):
            if i % 60 == 0:
                drift = rnd.choice([-1, 1]) * vol * rnd.uniform(0.02, 0.12)
            o = price
            c = o + drift + rnd.gauss(0, vol)
            out.append(candle(f"2026-09-21 {i:05d}", o, max(o, c) + abs(rnd.gauss(0, vol * 0.6)),
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
        self.assertEqual((t["status"], t["outcome"], t["result_r"]), ("closed", "win", 2.5))

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
            s = Storage(path)
            self.assertEqual(s.subscribers("scalp"), [42])
            s.toggle_style(42, "scalp")
            self.assertEqual(s.subscribers("scalp"), [])
            self.assertEqual(Storage(path).subscribers("swing"), [42])

    def test_stats(self):
        trades = [{"style": "scalp", "outcome": "win", "stage": 2, "result_r": 2.5},
                  {"style": "scalp", "outcome": "loss", "stage": 0, "result_r": -1},
                  {"style": "swing", "outcome": "expired", "stage": 0, "result_r": 0}]
        s = stats(trades)
        self.assertEqual((s["trades"], s["wins"], s["win_rate"], s["total_r"], s["expired"]), (2, 1, 50, 1.5, 1))


class MarketHoursTests(unittest.TestCase):
    def test_hours(self):
        utc = timezone.utc
        self.assertTrue(is_market_open(datetime(2026, 9, 23, 12, tzinfo=utc)))       # Wednesday
        self.assertFalse(is_market_open(datetime(2026, 9, 26, 12, tzinfo=utc)))      # Saturday
        self.assertFalse(is_market_open(datetime(2026, 9, 25, 21, 30, tzinfo=utc)))  # Friday late
        self.assertTrue(is_market_open(datetime(2026, 9, 27, 22, 30, tzinfo=utc)))   # Sunday open


class FakeModels:
    def __init__(self, head_decision="TAKE", fail=False):
        self.head_decision, self.fail, self.calls = head_decision, fail, 0

    async def generate_content(self, model, contents, config):
        self.calls += 1
        if self.fail:
            raise RuntimeError("quota")
        if "Head Trader" in contents:
            return types.SimpleNamespace(text=json.dumps({
                "decision": self.head_decision, "confidence": 81, "entry": "bad", "headline": "Sweep + OB retest",
                "reason": "All desks aligned."}))
        return types.SimpleNamespace(text='```json\n{"vote": "TAKE", "score": 75, "summary": "fine", "points": ["a"]}\n```')


def fake_desk(**kw):
    from agents import ModelPool
    desk = TradingDesk.__new__(TradingDesk)
    desk.pool, desk.min_rr, desk.min_confidence, desk.min_votes = ModelPool(["m1", "m2"], rpm=50), 1.5, 70, 3
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
        self.assertEqual(v["votes"], 5)
        self.assertIsNone(v["levels"])  # head trader's levels were invalid -> engine levels
        self.assertEqual(desk.fake.calls, 6)

    def test_desk_skip_and_down(self):
        setup, market, _ = first_setup()
        self.assertFalse(asyncio.run(fake_desk(head_decision="SKIP").review(setup, market, SESSION))["approved"])
        v = asyncio.run(fake_desk(fail=True).review(setup, market, SESSION))
        self.assertTrue(v.get("ai_down"))


class ModelPoolTests(unittest.TestCase):
    def test_spreads_and_skips_rate_limited_models(self):
        from agents import ModelPool

        async def run():
            pool = ModelPool(["a", "b"], rpm=2)
            got = [await pool.acquire("a", set()) for _ in range(4)]
            pool.penalize("a", RuntimeError("429 RESOURCE_EXHAUSTED"))
            cooling = pool._ready_at("a", time.monotonic() - 50) > time.monotonic()
            return got, cooling, await pool.acquire("a", {"b"})  # "a" is cooling -> skipped, not awaited
        got, cooling, none_left = asyncio.run(run())
        self.assertEqual(got, ["a", "a", "b", "b"])
        self.assertTrue(cooling)
        self.assertIsNone(none_left)


class UITests(unittest.TestCase):
    def test_signal_card_and_updates_render(self):
        t, _ = make_trade()
        t["reports"] = [{"icon": "🏗", "vote": "TAKE", "name": "S", "score": 80, "summary": "<ok>", "points": []}]
        t["reason"] = "Sweep & <OB>"
        card = ui.signal_card(t)
        self.assertIn("BUY LIMIT", card)
        self.assertIn("&lt;OB&gt;", card)
        self.assertIn("TP3", card)
        for ev in ({"kind": "filled", "price": 100}, {"kind": "tp", "n": 1, "price": 103, "rr": 1.5, "new_sl": 100},
                   {"kind": "tp", "n": 3, "price": 108, "rr": 4}, {"kind": "sl", "price": 98},
                   {"kind": "protected_stop", "price": 100, "stage": 1}, {"kind": "expired"}, {"kind": "cancelled"}):
            self.assertTrue(ui.event_message(t, ev))
        self.assertIn("TP1", ui.trade_status(t, 101))
        self.assertIn("AI Desk Report", ui.ai_report(t))

    def test_dashboard_renders(self):
        _, market, _ = first_setup()
        self.assertIn("Market Now", ui.market_dashboard(market, SESSION, True, datetime.now(timezone.utc)))


class EndToEndTests(unittest.TestCase):
    def test_scan_publishes_and_tracks(self):
        os.environ.update(TELEGRAM_BOT_TOKEN="1:x", GEMINI_API_KEY="x", TWELVEDATA_API_KEY="x")
        import bot as botmod
        from config import load_config

        setup, market, data = first_setup()
        sent = []

        class FakeBot:
            async def send_message(self, chat_id, text, **kw):
                sent.append((chat_id, text, kw))
                return types.SimpleNamespace(message_id=len(sent))

        with tempfile.TemporaryDirectory() as d:
            os.environ["DATA_FILE"] = os.path.join(d, "data.json")
            os.environ["STYLES"] = setup["style"]
            os.environ["MIN_ENGINE_SCORE"] = "0"
            try:
                gb = botmod.GoldBot(load_config())
            finally:
                for k in ("DATA_FILE", "STYLES", "MIN_ENGINE_SCORE"):
                    os.environ.pop(k)
            gb.desk = fake_desk()
            gb.storage.set_subscribed(111, True)

            async def fake_get():
                return data, {}
            gb.data.get = fake_get

            notes = asyncio.run(gb.scan(FakeBot(), manual=True))
            self.assertIn("signal sent", " ".join(notes))
            self.assertEqual(sent[0][0], 111)
            self.assertIn("XAU/USD", sent[0][1])
            trade = gb.storage.open_trades()[0]

            # Every menu screen renders.
            for key in ("menu", "trades", "hist", "perf", "mkt", "set", "help"):
                text, kb = asyncio.run(gb.screen(key, 111, 111))
                self.assertTrue(text and kb)
            self.assertIn("AI Desk", ui.ai_report(trade))

            # Same setup again is not re-sent.
            asyncio.run(gb.scan(FakeBot(), manual=True))
            self.assertEqual(len(sent), 1)

            # Price hits the stop on a new candle -> reply to the signal message.
            bull = trade["direction"] == "BUY"
            level = trade["stop_loss"] - 1 if bull else trade["stop_loss"] + 1
            m5 = data["5min"]
            m5.append(candle("9999-12-31 23:59:00", level, max(level, trade["entry"]), min(level, trade["entry"]), level))
            asyncio.run(gb.scan(FakeBot(), manual=True))
            replies = [s for s in sent[1:] if s[2].get("reply_to_message_id") == 1]
            self.assertTrue(replies)
            self.assertEqual(gb.storage.trade(trade["id"])["status"], "closed")


if __name__ == "__main__":
    unittest.main()
