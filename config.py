"""Settings loaded from environment variables (or a local .env file)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

import news

load_dotenv()


# Other free AI APIs (OpenAI-compatible). Keys from .env; each key gets its own slots in the model pool.
PROVIDERS = {
    "Mistral": {"base": "https://api.mistral.ai/v1", "env": "MISTRAL",
                "models": "mistral-large-latest,mistral-medium-latest,mistral-small-latest", "interval": 1.1},
    "Groq": {"base": "https://api.groq.com/openai/v1", "env": "GROQ",
             "models": "openai/gpt-oss-120b,openai/gpt-oss-20b", "interval": 2.1},
}


def _providers() -> list[dict]:
    out = []
    for name, p in PROVIDERS.items():
        keys = list(dict.fromkeys(_list(os.getenv(f"{p['env']}_API_KEY", "")) + _list(os.getenv(f"{p['env']}_API_KEYS", ""))))
        models = _list(os.getenv(f"{p['env']}_MODELS", p["models"]))
        for k in keys:
            out.append({"name": name, "base": p["base"], "key": k, "models": models, "interval": p["interval"]})
    return out


def _list(raw: str) -> list[str]:
    return [x for x in raw.replace(" ", "").split(",") if x]


def _markets(raw: str) -> list[str]:
    keys = [k.upper().replace("/", "") for k in _list(raw)] or ["XAUUSD"]
    unknown = set(keys) - {"XAUUSD", "BTCUSD"}
    if unknown:
        raise SystemExit(f"Unknown MARKETS in .env: {', '.join(unknown)} (use XAUUSD, BTCUSD)")
    return list(dict.fromkeys(keys))


@dataclass(frozen=True)
class Config:
    telegram_token: str
    gemini_api_key: str
    gemini_api_keys: list[str]
    twelvedata_api_key: str
    gemini_model: str
    gemini_fallback_models: list[str]
    gemini_rpm_per_model: int
    ai_providers: list
    explain_language: str
    min_conviction: int
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
    dashboard_port: int
    dashboard_host: str
    dashboard_open: bool
    strict_mode: bool
    news_feeds: tuple
    markets: list[str]


def load_config() -> Config:
    missing = [
        name
        for name in ("TELEGRAM_BOT_TOKEN", "TWELVEDATA_API_KEY")
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

    # Gemini is optional: leave GEMINI_API_KEY empty (or set USE_GEMINI=off) to run only on Mistral / Groq.
    gemini_keys = [] if os.getenv("USE_GEMINI", "on").lower() in ("0", "off", "false", "no") else \
        list(dict.fromkeys(_list(os.getenv("GEMINI_API_KEY", "")) + _list(os.getenv("GEMINI_API_KEYS", ""))))
    if not gemini_keys and not _providers():
        raise SystemExit("No AI key found. Put MISTRAL_API_KEY (recommended), GROQ_API_KEY or GEMINI_API_KEY in .env")

    return Config(
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
        gemini_api_key=gemini_keys[0] if gemini_keys else "",
        # Extra keys (comma separated) multiply the free quota; the bot rotates between all of them.
        gemini_api_keys=gemini_keys,
        twelvedata_api_key=os.environ["TWELVEDATA_API_KEY"],
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        gemini_fallback_models=_list(os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash,gemini-flash-latest,gemini-3-flash-preview,gemini-3.7-flash,"
                                                           "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-lite-latest")),
        gemini_rpm_per_model=int(os.getenv("GEMINI_RPM_PER_MODEL", "4")),
        ai_providers=_providers(),
        # Language of the AI's plain explanation on signals and market views, e.g. "Roman Urdu" or "English".
        explain_language=os.getenv("EXPLAIN_LANGUAGE", "simple English"),
        # Weighted average of every agent's score (0-100) the desk needs before a signal is sent.
        min_conviction=int(os.getenv("MIN_CONVICTION", "58")),
        symbol=os.getenv("SYMBOL", "XAU/USD"),
        volume_symbol=os.getenv("VOLUME_SYMBOL", "PAXGUSDT"),
        styles=styles,
        scan_interval_minutes=int(os.getenv("SCAN_INTERVAL_MINUTES", "5")),
        min_engine_score=int(os.getenv("MIN_ENGINE_SCORE", "55")),
        min_confidence=int(os.getenv("MIN_CONFIDENCE", "70")),
        min_agent_votes=int(os.getenv("MIN_AGENT_VOTES", "5")),
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
        news_feeds=tuple(dict.fromkeys(_list(os.getenv("NEWS_FEEDS", "")) + list(news.DEFAULT_FEEDS))),
        markets=_markets(os.getenv("MARKETS", "XAUUSD,BTCUSD")),
        # Strict engine mode (every filter must agree) finds very few setups; the 26-agent desk is the quality
        # filter now, so the engine runs in normal mode unless STRICT_MODE=on.
        strict_mode=os.getenv("STRICT_MODE", "off").lower() in ("1", "on", "true", "yes"),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8080")),
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        # Open the dashboard in the browser automatically on Windows desktops.
        dashboard_open=os.getenv("DASHBOARD_OPEN", "on" if os.name == "nt" else "off").lower()
        not in ("0", "off", "false", "no"),
    )
