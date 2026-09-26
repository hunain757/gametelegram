"""Tradable instruments and their market-specific settings."""

from datetime import datetime

import sessions

INSTRUMENTS = {
    "XAUUSD": {
        "key": "XAUUSD", "name": "XAU/USD", "label": "Gold", "icon": "XAU", "ai_name": "XAU/USD (gold)",
        # Candles from Twelve Data; volume from PAXG/USDT (tokenized gold) on Binance.
        "source": "twelvedata", "symbol": "XAU/USD", "volume_symbol": "PAXGUSDT",
        "pip": 0.1, "contract": 100, "unit": "oz", "always_open": False,
    },
    "BTCUSD": {
        "key": "BTCUSD", "name": "BTC/USD", "label": "Bitcoin", "icon": "BTC", "ai_name": "BTC/USD (bitcoin)",
        # Candles and real volume straight from Binance (free, no key, trades 24/7).
        "source": "binance", "symbol": "BTCUSDT", "volume_symbol": "BTCUSDT",
        "pip": 1.0, "contract": 1, "unit": "BTC", "always_open": True,
    },
}


def get(key: str | None) -> dict:
    return INSTRUMENTS.get(key or "XAUUSD", INSTRUMENTS["XAUUSD"])


def is_open(inst: dict, now: datetime | None = None) -> bool:
    return inst["always_open"] or sessions.is_market_open(now)


def drop_closed(candles: list[dict], tf: str) -> list[dict]:
    """Remove candles printed while gold is closed (Fri 21:00 → Sun 22:00 UTC).

    Some feeds keep printing flat candles over the weekend; they would flatten ATR/ADR, fake
    "consolidation" and mislead both the engine and the AI agents.
    """
    out = []
    for c in candles:
        dt = datetime.strptime(c["time"][:19], "%Y-%m-%d %H:%M:%S")
        if tf == "1day":
            if dt.weekday() == 5:  # Saturday
                continue
        elif not sessions.is_market_open(dt):
            continue
        out.append(c)
    return out
