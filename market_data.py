"""Gold (XAU/USD) price data from the Twelve Data API."""

from datetime import datetime, timezone

import httpx

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

# Timeframes the bot scans, from lowest to highest.
TIMEFRAMES = ("15min", "1h", "4h")


class MarketDataError(Exception):
    pass


async def fetch_candles(
    client: httpx.AsyncClient, api_key: str, symbol: str, interval: str, outputsize: int = 250
) -> list[dict]:
    """Return candles for one timeframe, oldest first."""
    resp = await client.get(
        TWELVE_DATA_URL,
        params={"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": api_key},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise MarketDataError(f"Twelve Data ({interval}): {data.get('message', 'unknown error')}")

    return [
        {
            "time": v["datetime"],
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
        }
        for v in reversed(data["values"])
    ]


async def fetch_all_timeframes(api_key: str, symbol: str) -> dict[str, list[dict]]:
    async with httpx.AsyncClient() as client:
        return {tf: await fetch_candles(client, api_key, symbol, tf) for tf in TIMEFRAMES}


def is_market_open(now: datetime | None = None) -> bool:
    """Gold trades from Sunday ~22:00 UTC to Friday ~21:00 UTC."""
    now = now or datetime.now(timezone.utc)
    weekday, hour = now.weekday(), now.hour  # Monday = 0 ... Sunday = 6
    if weekday == 5:
        return False
    if weekday == 4 and hour >= 21:
        return False
    if weekday == 6 and hour < 22:
        return False
    return True
