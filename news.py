"""Economic calendar (ForexFactory weekly feed): high-impact USD news moves gold violently.

The bot pauses new signals around high-impact events and warns users shortly before them.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger("goldbot.news")

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def parse(items: list[dict], currencies: tuple[str, ...]) -> list[dict]:
    events = []
    for it in items:
        if it.get("country") not in currencies or it.get("impact") not in ("High", "Medium"):
            continue
        try:
            when = datetime.fromisoformat(it["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        events.append({"title": it.get("title", "?"), "country": it["country"], "impact": it["impact"],
                       "time": when, "forecast": it.get("forecast") or "", "previous": it.get("previous") or ""})
    return sorted(events, key=lambda e: e["time"])


class NewsCalendar:
    def __init__(self, currencies: tuple[str, ...] = ("USD",), url: str = FF_URL, ttl: int = 3600):
        self.currencies = currencies
        self.url = url
        self.ttl = ttl
        self.events: list[dict] = []
        self.error: str | None = None
        self._fetched = 0.0
        self._alerted: set[str] = set()

    async def refresh(self):
        if time.time() - self._fetched < self.ttl:
            return
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(self.url, timeout=20, headers={"User-Agent": "Mozilla/5.0 goldbot"})
                resp.raise_for_status()
                self.events = parse(resp.json(), self.currencies)
            self.error = None
            self._fetched = time.time()
        except Exception as e:
            self.error = str(e)[:200]
            self._fetched = time.time() - self.ttl + 900  # retry in 15 min, keep old events
            log.warning("News calendar unavailable: %s", e)

    def upcoming(self, now: datetime | None = None, hours: float = 24, high_only: bool = False) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        return [e for e in self.events
                if now - timedelta(minutes=30) <= e["time"] <= now + timedelta(hours=hours)
                and (not high_only or e["impact"] == "High")]

    def blackout(self, now: datetime | None = None, before: int = 30, after: int = 30) -> dict | None:
        """The high-impact event whose window [time - before, time + after] contains now."""
        now = now or datetime.now(timezone.utc)
        for e in self.events:
            if e["impact"] == "High" and e["time"] - timedelta(minutes=before) <= now <= e["time"] + timedelta(minutes=after):
                return e
        return None

    def due_alerts(self, now: datetime | None = None, minutes: int = 20) -> list[dict]:
        """High-impact events starting within `minutes` that have not been announced yet."""
        now = now or datetime.now(timezone.utc)
        due = []
        for e in self.events:
            key = f"{e['title']}|{e['time'].isoformat()}"
            if e["impact"] == "High" and now <= e["time"] <= now + timedelta(minutes=minutes) and key not in self._alerted:
                self._alerted.add(key)
                due.append(e)
        return due

    def brief(self, now: datetime | None = None, hours: float = 8) -> list[str]:
        """Short lines for the AI agents' context."""
        now = now or datetime.now(timezone.utc)
        return [f"{e['impact']} {e['country']} {e['title']} at {e['time'].strftime('%H:%M UTC')}"
                f" (in {int((e['time'] - now).total_seconds() // 60)} min)"
                for e in self.upcoming(now, hours)]


# ---------------- headlines ----------------

DEFAULT_FEEDS = ("https://www.fxstreet.com/rss/news", "https://www.forexlive.com/feed/news",
                 "https://feeds.bbci.co.uk/news/world/rss.xml", "https://www.coindesk.com/arc/outboundfeeds/rss/")
# Headline categories: each news agent reads only its own kind of story.
CATEGORIES = {
    "gold": r"gold|xau|bullion|precious metals?|silver",
    "crypto": r"bitcoin|btc|crypto\w*|ether(?:eum)?|etf flows?|stablecoins?|binance|coinbase|sec\b",
    "macro": r"fed|fomc|powell|inflation|cpi|ppi|pce|nfp|payrolls|jobs|unemployment|gdp|dollar|usd|dxy|yields?|"
             r"treasur(?:y|ies)|rate cuts?|rate hikes?|interest rates?|ecb|boj|recession|central banks?",
    "world": r"geopolit\w*|wars?|tariffs?|sanctions?|conflict|missiles?|strikes?|attacks?|invasion|ceasefire|"
             r"elections?|oil|opec|china|russia|ukraine|iran|israel|gaza|middle east|taiwan|north korea|crisis|"
             r"trump|white house|nato|terror\w*|coup|protests?",
}
_CAT_RE = {k: re.compile(rf"\b({v})\b", re.I) for k, v in CATEGORIES.items()}
KEYWORDS = re.compile(r"\b(" + "|".join(CATEGORIES.values()) + r")\b", re.I)


def categorize(title: str) -> list[str]:
    return [k for k, rx in _CAT_RE.items() if rx.search(title)]


def parse_rss(xml_text: str, source: str) -> list[dict]:
    out = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title or not KEYWORDS.search(title):
            continue
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            when = None
        out.append({"title": title, "time": when, "source": source, "link": (item.findtext("link") or "").strip(),
                    "gold": bool(_CAT_RE["gold"].search(title)), "categories": categorize(title)})
    return out


class Headlines:
    def __init__(self, feeds: tuple[str, ...] = DEFAULT_FEEDS, ttl: int = 900):
        self.feeds = feeds
        self.ttl = ttl
        self.items: list[dict] = []
        self.error: str | None = None
        self._fetched = 0.0

    async def refresh(self):
        if not self.feeds or time.time() - self._fetched < self.ttl:
            return
        self._fetched = time.time()
        items, errors = [], []
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for url in self.feeds:
                try:
                    resp = await client.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 goldbot"})
                    resp.raise_for_status()
                    items += parse_rss(resp.text, re.sub(r"^https?://(www\.)?([^/]+).*", r"\2", url))
                except Exception as e:
                    errors.append(f"{url}: {str(e)[:80]}")
        if items:
            seen, uniq = set(), []
            for it in sorted(items, key=lambda x: x["time"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
                if it["title"].lower() not in seen:
                    seen.add(it["title"].lower())
                    uniq.append(it)
            self.items = uniq[:60]
        self.error = "; ".join(errors) if errors and not items else None
        if errors:
            log.warning("Headline feeds: %s", "; ".join(errors))

    def latest(self, n: int = 8, hours: float = 24) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return [h for h in self.items if not h["time"] or h["time"] >= cutoff][:n]

    def brief(self, n: int = 6, category: str | None = None) -> list[str]:
        items = [h for h in self.latest(60) if not category or category in h.get("categories", [])][:n]
        return [f"{h['time'].strftime('%H:%M') if h['time'] else '--:--'} UTC {h['source']}: {h['title']}"
                for h in items]

    def by_category(self, n: int = 6) -> dict[str, list[str]]:
        return {c: self.brief(n, c) for c in CATEGORIES}
