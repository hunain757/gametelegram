"""JSON-file store for users, their preferences, trades and de-duplication keys."""

import json
import os
from datetime import datetime, timedelta, timezone

from setups import STYLES

MAX_TRADES = 500


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.data = {"users": {}, "trades": [], "seen": {}}
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            # Older versions stored a plain subscriber list.
            for chat_id in loaded.pop("subscribers", []):
                loaded.setdefault("users", {})[str(chat_id)] = self._new_user()
            loaded.pop("last_signal", None)
            self.data.update(loaded)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    # ---------- users ----------

    @staticmethod
    def _new_user() -> dict:
        return {"subscribed": True, "styles": list(STYLES)}

    def user(self, chat_id: int) -> dict:
        key = str(chat_id)
        if key not in self.data["users"]:
            self.data["users"][key] = self._new_user()
            self.save()
        return self.data["users"][key]

    def set_subscribed(self, chat_id: int, on: bool):
        self.user(chat_id)["subscribed"] = on
        self.save()

    def toggle_style(self, chat_id: int, style: str):
        styles = self.user(chat_id)["styles"]
        if style in styles:
            styles.remove(style)
        else:
            styles.append(style)
        self.save()

    def subscribers(self, style: str | None = None) -> list[int]:
        return [int(k) for k, u in self.data["users"].items()
                if u.get("subscribed") and (style is None or style in u.get("styles", []))]

    # ---------- trades ----------

    def add_trade(self, trade: dict):
        self.data["trades"].append(trade)
        closed = [t for t in self.data["trades"] if t["status"] == "closed"]
        if len(closed) > MAX_TRADES:
            drop = {t["id"] for t in closed[: len(closed) - MAX_TRADES]}
            self.data["trades"] = [t for t in self.data["trades"] if t["id"] not in drop]
        self.save()

    def trade(self, trade_id: str) -> dict | None:
        return next((t for t in self.data["trades"] if t["id"] == trade_id), None)

    def open_trades(self) -> list[dict]:
        return [t for t in self.data["trades"] if t["status"] != "closed"]

    def closed_trades(self, since: datetime | None = None) -> list[dict]:
        trades = [t for t in self.data["trades"] if t["status"] == "closed"]
        if since:
            trades = [t for t in trades if datetime.fromisoformat(t["closed_at"]) >= since]
        return trades

    # ---------- de-duplication ----------

    def seen(self, key: str, hours: float = 12) -> bool:
        ts = self.data["seen"].get(key)
        return bool(ts) and datetime.now(timezone.utc) - datetime.fromisoformat(ts) < timedelta(hours=hours)

    def mark_seen(self, key: str):
        now = datetime.now(timezone.utc)
        self.data["seen"] = {k: v for k, v in self.data["seen"].items()
                             if now - datetime.fromisoformat(v) < timedelta(days=3)}
        self.data["seen"][key] = now.isoformat()
        self.save()


def stats(trades: list[dict]) -> dict:
    """Win rate and R for closed trades. A trade that reached TP1 counts as a win."""
    finished = [t for t in trades if t.get("outcome") in ("win", "loss", "breakeven")]
    wins = [t for t in finished if t["stage"] >= 1]
    losses = [t for t in finished if t["stage"] == 0]
    by_style = {}
    for s in STYLES:
        st = [t for t in finished if t["style"] == s]
        w = sum(t["stage"] >= 1 for t in st)
        by_style[s] = {"trades": len(st), "wins": w, "win_rate": round(100 * w / len(st)) if st else None}
    return {
        "trades": len(finished),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(finished)) if finished else None,
        "total_r": round(sum(t.get("result_r") or 0 for t in finished), 2),
        "tp3": sum(t["stage"] == 3 for t in finished),
        "expired": sum(t.get("outcome") in ("expired", "cancelled") for t in trades),
        "by_style": by_style,
    }
