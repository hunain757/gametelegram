"""Small text helpers shared by the app and the website state (no emoji anywhere)."""

import re

from setups import STYLES

_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣₿]")


def plain(text: str) -> str:
    """Text without emoji (trades stored by older versions still carry emoji style labels)."""
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", text or "")).strip()


def style_name(style: str) -> str:
    return plain(STYLES[style]["label"]) if style in STYLES else style


def label(trade: dict) -> str:
    return plain(trade.get("style_label", ""))


def lot_size(balance: float, risk_pct: float, sl_distance: float, contract: float = 100) -> tuple[float, float]:
    """Lots for a given account risk. Gold: 1 lot = `contract` oz, so a $1 move = $contract per lot."""
    risk_usd = balance * risk_pct / 100
    lots = int(risk_usd / (sl_distance * contract) * 100) / 100 if sl_distance > 0 else 0.0
    return max(lots, 0.0), risk_usd
