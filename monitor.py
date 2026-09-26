"""Live activity feed and agent status, shown on the local dashboard."""

from collections import deque
from datetime import datetime, timezone

AGENT_KEYS = ("structure", "liquidity", "volume", "price_action", "momentum", "session_news", "risk", "devil", "head")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class Monitor:
    def __init__(self):
        self.log: deque = deque(maxlen=400)
        self.reviews: deque = deque(maxlen=25)
        self.phase = "starting"
        self.agents = {k: {"status": "idle", "vote": None, "score": None, "summary": "", "points": [],
                           "model": None, "at": None, "seconds": None} for k in AGENT_KEYS}

    def event(self, text: str, kind: str = "info"):
        self.log.append({"t": _now(), "kind": kind, "text": text})

    def set_phase(self, phase: str):
        self.phase = phase

    def agent(self, key: str, status: str, **fields):
        a = self.agents.setdefault(key, {})
        a.update(status=status, at=_now(), **fields)

    def review(self, entry: dict):
        self.reviews.appendleft({"t": _now(), **entry})
