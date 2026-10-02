"""JSON-file store: trades, de-duplication keys, per-style settings, account settings and AI pool state."""

import json
import os
from datetime import datetime, timedelta, timezone

from setups import STYLES

MAX_TRADES = 500
KEEP = ("trades", "seen", "style_settings", "account", "ai_pool")


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.data = {"trades": [], "seen": {}, "style_settings": {}, "account": {"balance": None, "risk": 1.0},
                     "ai_pool": {}}
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if self._clean(loaded):
                self.data.update({k: loaded[k] for k in KEEP if k in loaded})
                self.save()
            else:
                self.data.update({k: loaded[k] for k in KEEP if k in loaded})

    @staticmethod
    def _clean(loaded: dict) -> bool:
        """Drop what older (Telegram) versions stored: users, subscribers, owner, message ids, stale keys.
        Returns True when something was removed so the cleaned file is written back once."""
        changed = False
        for k in list(loaded):
            if k not in KEEP:
                loaded.pop(k)
                changed = True
        for t in loaded.get("trades", []):
            if "messages" in t:
                t.pop("messages")
                changed = True
        now = datetime.now(timezone.utc)
        seen = loaded.get("seen", {})
        fresh = {k: v for k, v in seen.items() if now - datetime.fromisoformat(v) < timedelta(days=3)}
        if len(fresh) != len(seen):
            loaded["seen"] = fresh
            changed = True
        return changed

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    # ---------- account (lot size on the website) ----------

    @property
    def account(self) -> dict:
        return self.data.setdefault("account", {"balance": None, "risk": 1.0})

    def set_account(self, balance: float | None = None, risk: float | None = None):
        if balance is not None:
            self.account["balance"] = balance if balance > 0 else None
        if risk is not None:
            self.account["risk"] = min(max(risk, 0.1), 10.0)
        self.save()

    # ---------- per-style strategy settings ----------

    def style_settings(self, style: str) -> dict:
        return dict(self.data.get("style_settings", {}).get(style, {}))

    def set_style_settings(self, style: str, **values):
        self.data.setdefault("style_settings", {}).setdefault(style, {}).update(values)
        self.save()

    def reset_style_settings(self, style: str):
        self.data.setdefault("style_settings", {}).pop(style, None)
        self.save()

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
    """Win rate, R, drawdown and equity curve for closed trades. A trade that reached TP1 counts as a win."""
    finished = [t for t in trades if t.get("outcome") in ("win", "loss", "breakeven")]
    wins = [t for t in finished if t["stage"] >= 1]
    losses = [t for t in finished if t["stage"] == 0]
    by_style = {}
    for s in STYLES:
        st = [t for t in finished if t["style"] == s]
        w = sum(t["stage"] >= 1 for t in st)
        by_style[s] = {"trades": len(st), "wins": w, "win_rate": round(100 * w / len(st)) if st else None,
                       "total_r": round(sum(t.get("result_r") or 0 for t in st), 2)}
    by_instrument = {}
    for t in finished:
        k = t.get("instrument", "XAUUSD")
        x = by_instrument.setdefault(k, {"trades": 0, "wins": 0, "total_r": 0.0})
        x["trades"] += 1
        x["wins"] += t["stage"] >= 1
        x["total_r"] = round(x["total_r"] + (t.get("result_r") or 0), 2)
    equity, peak, dd, curve = 0.0, 0.0, 0.0, []
    for t in sorted(finished, key=lambda t: t.get("closed_at") or t.get("created_at") or ""):
        equity += t.get("result_r") or 0
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
        curve.append({"t": (t.get("closed_at") or t.get("created_at") or "")[:16], "r": round(equity, 2)})
    gains = sum(t["result_r"] for t in finished if (t.get("result_r") or 0) > 0)
    pains = -sum(t["result_r"] for t in finished if (t.get("result_r") or 0) < 0)
    return {
        "trades": len(finished),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(finished)) if finished else None,
        "total_r": round(sum(t.get("result_r") or 0 for t in finished), 2),
        "avg_r": round(sum(t.get("result_r") or 0 for t in finished) / len(finished), 2) if finished else None,
        "profit_factor": round(gains / pains, 2) if pains else None,
        "max_dd": round(dd, 2),
        "tp3": sum(t["stage"] == 3 for t in finished),
        "expired": sum(t.get("outcome") in ("expired", "cancelled") for t in trades),
        "by_style": by_style,
        "by_instrument": by_instrument,
        "equity": curve[-200:],
    }
