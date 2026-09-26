"""Settings loaded from environment variables (or a local .env file)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _list(raw: str) -> list[str]:
    return [x for x in raw.replace(" ", "").split(",") if x]


@dataclass(frozen=True)
class Config:
    telegram_token: str
    gemini_api_key: str
    twelvedata_api_key: str
    gemini_model: str
    gemini_fallback_models: list[str]
    gemini_rpm_per_model: int
    symbol: str
    volume_symbol: str
    styles: list[str]
    scan_interval_minutes: int
    min_engine_score: int
    min_confidence: int
    min_agent_votes: int
    min_risk_reward: float
    engine_only_score: int
    daily_report_hour: int
    channel_id: str
    admin_ids: list[int]
    data_file: str
    proxy_url: str
    contract_size: float
    news_blackout_min: int
    news_currencies: tuple
    briefings: bool


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

    styles = _list(os.getenv("STYLES", "scalp,intraday,swing"))
    unknown = set(styles) - {"scalp", "intraday", "swing"}
    if unknown:
        raise SystemExit(f"Unknown STYLES in .env: {', '.join(unknown)} (use scalp, intraday, swing)")

    return Config(
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
        gemini_api_key=os.environ["GEMINI_API_KEY"],
        twelvedata_api_key=os.environ["TWELVEDATA_API_KEY"],
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        gemini_fallback_models=_list(os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash,gemini-flash-latest,gemini-3-flash-preview,gemini-3.7-flash")),
        gemini_rpm_per_model=int(os.getenv("GEMINI_RPM_PER_MODEL", "4")),
        symbol=os.getenv("SYMBOL", "XAU/USD"),
        volume_symbol=os.getenv("VOLUME_SYMBOL", "PAXGUSDT"),
        styles=styles,
        scan_interval_minutes=int(os.getenv("SCAN_INTERVAL_MINUTES", "5")),
        min_engine_score=int(os.getenv("MIN_ENGINE_SCORE", "55")),
        min_confidence=int(os.getenv("MIN_CONFIDENCE", "70")),
        min_agent_votes=int(os.getenv("MIN_AGENT_VOTES", "3")),
        min_risk_reward=float(os.getenv("MIN_RISK_REWARD", "1.5")),
        engine_only_score=int(os.getenv("ENGINE_ONLY_SCORE", "85")),
        daily_report_hour=int(os.getenv("DAILY_REPORT_HOUR_UTC", "21")),
        channel_id=os.getenv("CHANNEL_ID", ""),
        admin_ids=[int(x) for x in _list(os.getenv("ADMIN_IDS", ""))],
        data_file=os.getenv("DATA_FILE", "data.json"),
        proxy_url=os.getenv("PROXY_URL", ""),
        contract_size=float(os.getenv("CONTRACT_SIZE", "100")),
        news_blackout_min=int(os.getenv("NEWS_BLACKOUT_MIN", "30")),
        news_currencies=tuple(_list(os.getenv("NEWS_CURRENCIES", "USD"))),
        briefings=os.getenv("BRIEFINGS", "on").lower() not in ("0", "off", "false", "no"),
    )
