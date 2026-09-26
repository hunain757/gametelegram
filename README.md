# Gold AI Signal Bot (Telegram)

A Telegram bot that scans the **gold market (XAU/USD)** with Google Gemini AI and sends a
BUY/SELL signal **only when there is a strong trade**. If there is no good trade, it stays silent.

## How it works

1. Every 15 minutes (configurable) it fetches gold candles for **15min, 1h and 4h** from Twelve Data.
2. It computes EMA 20/50/200, RSI, ATR, MACD and recent swing high/low for each timeframe.
3. It sends this data to **Gemini**, which answers "trade / no trade" with entry, SL, TP1, TP2 and confidence.
4. The bot double-checks the AI answer. It only sends the signal if **all** of these pass:
   - Confidence ≥ `MIN_CONFIDENCE` (default 70%)
   - Levels are in the right order (e.g. for BUY: SL < Entry < TP1 ≤ TP2)
   - Risk:Reward ≥ `MIN_RISK_REWARD` (default 1.5)
   - Entry is close to the current price
   - No other signal was sent in the last `COOLDOWN_MINUTES`
5. It skips scanning while the gold market is closed (weekend).

## Keys you need

| Key | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `TWELVEDATA_API_KEY` | https://twelvedata.com (the free plan is enough) |

## Run it on Windows (easiest)

1. Install Python from https://www.python.org/downloads/ (tick **"Add python.exe to PATH"**).
2. Download this repo (green **Code** button → **Download ZIP**) and unzip it.
3. Double-click **`start.bat`**. The first time, it installs everything and asks you to paste
   your 3 keys; it saves them in a local `.env` file. After that it just starts the bot.

To change a key later, delete `.env` and run `start.bat` again (or edit `.env` in Notepad).

## Run it on your PC (manual)

```bash
git clone https://github.com/hunain757/gametelegram.git
cd gametelegram
python -m venv venv
# Windows: venv\Scripts\activate    |  Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env      # Linux/Mac: cp .env.example .env
# Open .env and paste your 3 keys
python bot.py
```

Then open your bot on Telegram and send `/start`.

> ⚠️ Never upload `.env` to GitHub. It is already in `.gitignore`.

## Commands

| Command | What it does |
|---|---|
| `/start` | Subscribe to signals |
| `/stop` | Unsubscribe |
| `/status` | Market open/closed, last scan result and the AI's view |
| `/last` | Show the last signal sent |
| `/scan` | Scan right now (only `ADMIN_IDS` if set) |

## Settings (`.env`)

See `.env.example`. You can also set `CHANNEL_ID` to post signals to a Telegram channel
(add the bot as an admin of the channel first).

## Tests

```bash
python -m unittest discover -s tests -t .
```

---
⚠️ These signals are not financial advice. AI can be wrong. Always use a stop loss and proper risk management.
