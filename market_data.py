"""Market data: XAU/USD candles from Twelve Data, volume from Binance PAXG/USDT.

Higher timeframes change slowly, so each timeframe is cached for a while. This keeps
a 5-minute scan loop inside the Twelve Data free plan (800 requests/day, 8/minute).
"""

import logging
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("goldbot.data")

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"

TIMEFRAMES = ("5min", "15min", "1h", "4h", "1day")
# Seconds to reuse a timeframe before fetching it again (~450 requests/day in total).
CACHE_TTL = {"5min": 240, "15min": 840, "1h": 1740, "4h": 7000, "1day": 21000}
BINANCE_INTERVAL = {"5min": "5m", "15min": "15m", "1h": "1h", "4h": "4h", "1day": "1d"}


class MarketDataError(Exception):
    pass


async def fetch_candles(client: httpx.AsyncClient, api_key: str, symbol: str, interval: str,
                        outputsize: int = 300) -> list[dict]:
    """Twelve Data candles for one timeframe, oldest first, times in UTC."""
    resp = await client.get(TWELVE_DATA_URL, params={
        "symbol": symbol, "interval": interval, "outputsize": outputsize, "timezone": "UTC", "apikey": api_key,
    }, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise MarketDataError(f"Twelve Data ({interval}): {data.get('message', 'unknown error')}")
    return [
        {"time": v["datetime"] if " " in v["datetime"] else v["datetime"] + " 00:00:00",
         "open": float(v["open"]), "high": float(v["high"]), "low": float(v["low"]), "close": float(v["close"])}
        for v in reversed(data["values"])
    ]


async def fetch_volume_candles(client: httpx.AsyncClient, symbol: str, interval: str, limit: int = 300) -> list[dict]:
    """Binance klines (public, no key needed), oldest first."""
    resp = await client.get(BINANCE_URL, params={"symbol": symbol, "interval": BINANCE_INTERVAL[interval],
                                                 "limit": limit}, timeout=20)
    resp.raise_for_status()
    return [
        {"time": datetime.fromtimestamp(k[0] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
         "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
        for k in resp.json()
    ]


class MarketData:
    def __init__(self, api_key: str, symbol: str, volume_symbol: str = "PAXGUSDT"):
        self.api_key = api_key
        self.symbol = symbol
        self.volume_symbol = volume_symbol
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._vcache: dict[str, tuple[float, list[dict]]] = {}
        self._volume_off_until = 0.0
        self.requests_today = 0
        self._day = datetime.now(timezone.utc).date()

    async def get(self) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        """Candles and volume candles for every timeframe, fetching only what is stale."""
        now = time.time()
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day, self.requests_today = today, 0

        async with httpx.AsyncClient() as client:
            for tf in TIMEFRAMES:
                cached = self._cache.get(tf)
                if cached and now - cached[0] < CACHE_TTL[tf]:
                    continue
                try:
                    self._cache[tf] = (now, await fetch_candles(client, self.api_key, self.symbol, tf))
                    self.requests_today += 1
                except Exception as e:
                    if not cached or tf == "5min":
                        raise MarketDataError(str(e)) from e
                    log.warning("Using cached %s candles: %s", tf, e)

            if self.volume_symbol and now >= self._volume_off_until:
                try:
                    for tf in TIMEFRAMES:
                        cached = self._vcache.get(tf)
                        if not cached or now - cached[0] >= CACHE_TTL[tf]:
                            self._vcache[tf] = (now, await fetch_volume_candles(client, self.volume_symbol, tf))
                except Exception as e:
                    log.warning("Volume source unavailable, retrying in 30 min: %s", e)
                    self._volume_off_until = now + 1800

        candles = {tf: self._cache[tf][1] for tf in TIMEFRAMES}
        volumes = {tf: v[1] for tf, v in self._vcache.items()}
        return candles, volumes
