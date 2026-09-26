"""Asks Google Gemini whether the current gold market has a trade, then sanity-checks the answer."""

import json
import re

from google import genai
from google.genai import types

from indicators import summarize

PROMPT = """You are a strict, professional XAU/USD (gold) trader.
Below is live market data for {symbol} on several timeframes (15min, 1h, 4h),
with indicators and the most recent candles. Current price: {price}.

{data}

Decide whether there is a HIGH-PROBABILITY trade right now.
Rules:
- Only take a trade when the higher timeframes (4h, 1h) and the 15min entry agree
  (trend, EMA alignment, momentum, structure). If in doubt, do NOT trade.
- Entry must be close to the current price (a market entry).
- Stop loss goes beyond a logical swing level, roughly 1-2x the 15min ATR away.
- Take profit 1 must give at least {min_rr}x the risk; take profit 2 further out.
- confidence is 0-100 and must be honest; most scans should have no trade.

Reply with JSON only, in exactly this shape:
{{"trade": true or false,
  "direction": "BUY" or "SELL" or null,
  "entry": number or null,
  "stop_loss": number or null,
  "take_profit_1": number or null,
  "take_profit_2": number or null,
  "confidence": integer,
  "reason": "short explanation in simple English"}}
"""


class Analyzer:
    def __init__(self, api_key: str, model: str, symbol: str, min_rr: float):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.symbol = symbol
        self.min_rr = min_rr

    async def analyze(self, candles_by_tf: dict[str, list[dict]]) -> dict:
        price = candles_by_tf["15min"][-1]["close"]
        data = {tf: summarize(candles) for tf, candles in candles_by_tf.items()}
        prompt = PROMPT.format(
            symbol=self.symbol, price=round(price, 2), data=json.dumps(data, indent=1), min_rr=self.min_rr
        )
        resp = await self.client.aio.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2, response_mime_type="application/json"),
        )
        result = parse_json(resp.text or "")
        result["price"] = price
        return result


def parse_json(text: str) -> dict:
    """Parse the model's JSON, tolerating ```json fences around it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"AI did not return JSON: {text[:200]}")
    return json.loads(match.group(0))


def validate_signal(sig: dict, min_confidence: int, min_rr: float) -> tuple[bool, str]:
    """Reject AI answers that are not a clean, tradeable signal."""
    if not sig.get("trade"):
        return False, "AI found no trade"
    direction = str(sig.get("direction") or "").upper()
    if direction not in ("BUY", "SELL"):
        return False, f"bad direction {direction!r}"
    try:
        entry, sl = float(sig["entry"]), float(sig["stop_loss"])
        tp1, tp2 = float(sig["take_profit_1"]), float(sig["take_profit_2"])
        confidence = int(sig.get("confidence") or 0)
    except (KeyError, TypeError, ValueError):
        return False, "missing or non-numeric levels"

    if confidence < min_confidence:
        return False, f"confidence {confidence} < {min_confidence}"
    if direction == "BUY" and not (sl < entry < tp1 <= tp2):
        return False, "BUY levels out of order"
    if direction == "SELL" and not (sl > entry > tp1 >= tp2):
        return False, "SELL levels out of order"
    if abs(entry - sig["price"]) / sig["price"] > 0.005:
        return False, "entry too far from current price"
    rr = abs(tp1 - entry) / abs(entry - sl)
    if rr < min_rr:
        return False, f"risk/reward {rr:.2f} < {min_rr}"

    sig.update(direction=direction, entry=entry, stop_loss=sl, take_profit_1=tp1,
               take_profit_2=tp2, confidence=confidence, risk_reward=round(rr, 2))
    return True, "ok"
