import math
import unittest
from datetime import datetime, timezone

from analyzer import parse_json, validate_signal
from indicators import atr, ema, macd, rsi, summarize
from market_data import is_market_open


def make_candles(n=250, start=2400.0):
    candles = []
    for i in range(n):
        close = start + i * 0.5 + 5 * math.sin(i / 5)
        candles.append({"time": str(i), "open": close - 1, "high": close + 2, "low": close - 2, "close": close})
    return candles


class IndicatorTests(unittest.TestCase):
    def test_ema_constant_series(self):
        self.assertAlmostEqual(ema([5.0] * 30, 10)[-1], 5.0)
        self.assertIsNone(ema([1.0, 2.0], 10)[-1])

    def test_rsi_bounds(self):
        self.assertEqual(rsi([float(i) for i in range(30)]), 100.0)
        self.assertLess(rsi([float(30 - i) for i in range(30)]), 1)

    def test_atr_and_macd(self):
        candles = make_candles()
        self.assertGreater(atr(candles), 0)
        self.assertIsNotNone(macd([c["close"] for c in candles]))

    def test_summarize(self):
        s = summarize(make_candles())
        self.assertEqual(len(s["recent_candles"]), 12)
        self.assertIsNotNone(s["ema200"])


class SignalTests(unittest.TestCase):
    def good(self, **kw):
        sig = {"trade": True, "direction": "buy", "entry": 2500, "stop_loss": 2490,
               "take_profit_1": 2520, "take_profit_2": 2535, "confidence": 80, "price": 2501}
        sig.update(kw)
        return sig

    def test_valid_buy(self):
        sig = self.good()
        self.assertEqual(validate_signal(sig, 70, 1.5), (True, "ok"))
        self.assertEqual(sig["direction"], "BUY")
        self.assertEqual(sig["risk_reward"], 2.0)

    def test_rejections(self):
        self.assertFalse(validate_signal({"trade": False}, 70, 1.5)[0])
        self.assertFalse(validate_signal(self.good(confidence=50), 70, 1.5)[0])
        self.assertFalse(validate_signal(self.good(stop_loss=2510), 70, 1.5)[0])
        self.assertFalse(validate_signal(self.good(take_profit_1=2505), 70, 1.5)[0])
        self.assertFalse(validate_signal(self.good(entry=2600, take_profit_1=2700, take_profit_2=2800), 70, 1.5)[0])
        self.assertFalse(validate_signal(self.good(entry=None), 70, 1.5)[0])

    def test_valid_sell(self):
        sig = self.good(direction="SELL", stop_loss=2510, take_profit_1=2480, take_profit_2=2470)
        self.assertTrue(validate_signal(sig, 70, 1.5)[0])

    def test_parse_json_with_fences(self):
        self.assertEqual(parse_json('```json\n{"trade": false}\n```'), {"trade": False})


class MarketHoursTests(unittest.TestCase):
    def test_hours(self):
        utc = timezone.utc
        self.assertTrue(is_market_open(datetime(2026, 9, 23, 12, tzinfo=utc)))   # Wednesday
        self.assertFalse(is_market_open(datetime(2026, 9, 26, 12, tzinfo=utc)))  # Saturday
        self.assertFalse(is_market_open(datetime(2026, 9, 25, 21, 30, tzinfo=utc)))  # Friday late
        self.assertTrue(is_market_open(datetime(2026, 9, 27, 22, 30, tzinfo=utc)))   # Sunday open


if __name__ == "__main__":
    unittest.main()
