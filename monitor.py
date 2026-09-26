"""Live activity feed and agent status, shown on the local dashboard."""

import time
from collections import deque
from datetime import datetime, timezone

AGENT_KEYS = ("structure", "liquidity", "orderblocks", "imbalance", "volume", "price_action", "momentum", "trend",
              "volatility", "mtf", "ict", "levels", "trend_follow", "breakout_rev", "calendar", "world", "macro",
              "intermarket", "tech_lead", "strategy_lead", "macro_lead", "confluence", "risk", "devil", "head",
              "auditor")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class Monitor:
    def __init__(self):
        self.log: deque = deque(maxlen=400)
        self.reviews: deque = deque(maxlen=25)
        self.flows: deque = deque(maxlen=400)
        self._seq = 0
        self.phase = "starting"
        self.board = None  # the strategy board of the last review
        self.current = None  # the review in progress (or the last one), for the live-review panel
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

    def message(self, src: str, dst: str, text: str = "", kind: str = "report"):
        """Information passed from one agent (or the engine) to another: drawn as a moving line on the
        dashboard and listed in the agent conversation."""
        self._seq += 1
        self.flows.append({"id": self._seq, "from": src, "to": dst, "t": _now(), "ts": time.time(),
                           "text": text[:300], "kind": kind})

    def review_start(self, label: str, practice: bool = False):
        self.current = {"label": label, "practice": practice, "ts": time.time(), "end": None, "approved": None}

    def review_end(self, approved: bool):
        if self.current:
            self.current.update(end=time.time(), approved=approved)
