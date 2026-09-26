"""Settings loaded from environment variables (or a local .env file)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


@dataclass(frozen=True)
class Config:
    telegram_token: str
    gemini_api_key: str
    twelvedata_api_key: str
    gemini_model: str
    symbol: str
    scan_interval_minutes: int
    min_confidence: int
    min_risk_reward: float
    cooldown_minutes: int
    channel_id: str
    admin_ids: list[int]
    data_file: str


def load_config() -> Config:
    missing = [
        name
        for name in ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "TWELVEDATA_API_KEY")
        if not os.getenv(name)
    ]
    if missing:
        raise SystemExit(
            f"Missing required settings in .env: {', '.join(missing)}\n"
            "Open .env in Notepad and fill them in (or delete .env and run start.bat again)."
        )

    return Config(
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
        gemini_api_key=os.environ["GEMINI_API_KEY"],
        twelvedata_api_key=os.environ["TWELVEDATA_API_KEY"],
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        symbol=os.getenv("SYMBOL", "XAU/USD"),
        scan_interval_minutes=int(os.getenv("SCAN_INTERVAL_MINUTES", "15")),
        min_confidence=int(os.getenv("MIN_CONFIDENCE", "70")),
        min_risk_reward=float(os.getenv("MIN_RISK_REWARD", "1.5")),
        cooldown_minutes=int(os.getenv("COOLDOWN_MINUTES", "60")),
        channel_id=os.getenv("CHANNEL_ID", ""),
        admin_ids=_int_list(os.getenv("ADMIN_IDS", "")),
        data_file=os.getenv("DATA_FILE", "data.json"),
    )
