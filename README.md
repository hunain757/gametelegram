# 🏆 Gold SMC AI Signal Bot (Telegram)

A Telegram bot that scans **gold (XAU/USD)** around the clock with a **Smart Money Concepts engine**,
**volume analysis** and a **desk of 6 Gemini AI agents**, sends **scalping / intraday / swing** signals
with entry, SL and 3 TPs, and **tracks every trade live** (entry fill, TP1/TP2/TP3, SL, expiry).

## How it works

```
every 5 min ─► Twelve Data: XAU/USD M5 · M15 · H1 · H4 · D1   (+ Binance PAXG/USDT volume)
                │
                ▼
        SMC ENGINE (per timeframe)
        • swings, BOS / CHoCH, trend
        • order blocks, fair value gaps
        • liquidity pools, equal highs/lows, liquidity sweeps
        • premium / discount, sessions & killzones
        • volume: relative volume, delta (buy/sell pressure), POC & value area
                │
                ▼
        SETUP FINDER (per style)          bias TF → confirm TF → entry TF
        ⚡ Scalping   H1 → M15 → M5         trigger: CHoCH/BOS or liquidity sweep
        📊 Intraday   H4 → H1  → M15        entry: order block / FVG retest
        🌊 Swing      D1 → H4  → H1         SL: beyond POI, sweep wick and nearby liquidity
                │                            TPs: next liquidity pools (min 1:1.5)
                ▼ (only setups with confluence score ≥ MIN_ENGINE_SCORE)
        AI DESK (Gemini, each agent on its own model)
        🏗 Structure  💧 Liquidity/OB  📊 Volume  ⚙️ Momentum  🛡 Risk   → vote TAKE/SKIP
        👑 Head Trader reads all 5 reports → final TAKE/SKIP, confidence, fine-tuned levels
                │
                ▼ (Head Trader TAKE + confidence ≥ 70 + ≥ 3/5 agents agree)
        TELEGRAM signal card  ─►  live tracking replies on the signal:
        ✅ entry filled · 🎯 TP1 (move SL to BE) · 🎯 TP2 · 🏆 TP3 · 🛑 SL · ⌛ expired
```

## Telegram features

- **Signal cards** with entry / SL / TP1–TP3 (pips and R:R), AI confidence bar, each agent's vote,
  confluence list and the Head Trader's reasoning
- Buttons on each signal: **📍 Live Status** (floating pips, TPs hit) and **🧠 AI Desk Report** (every agent's analysis)
- **Menu** (`/start`): 📡 Active Trades · 📜 History · 📊 Performance (win rate, R) · 🌍 Market Now
  (structure on every TF, liquidity, order blocks, FVGs, volume) · 🧠 AI Market View · ⚙️ Signal Types
  (turn scalping / intraday / swing on or off) · 🔔 Alerts on/off · ⚡ Scan now (admin)
- **Daily report** (Mon–Fri) with win rate and total R
- Optional **channel** posting (`CHANNEL_ID`)

Commands: `/start` `/menu` `/trades` `/history` `/stats` `/market` `/scan` `/help` `/stop`

## Keys you need

| Key | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `TWELVEDATA_API_KEY` | https://twelvedata.com (the free plan is enough) |

Volume comes from Binance's public PAXG/USDT market data, which needs no key.

## Run it on Windows (easiest)

1. Install Python from https://www.python.org/downloads/ (tick **"Add python.exe to PATH"**).
2. Download this repo (green **Code** button → **Download ZIP**) and unzip it.
3. Double-click **`start.bat`**. The first time, it installs everything and asks you to paste
   your 3 keys; it saves them in a local `.env` file. After that it just starts the bot.

To change a key later, delete `.env` and run `start.bat` again (or edit `.env` in Notepad).

> If Telegram is blocked on your internet, turn on a VPN (e.g. Cloudflare WARP) before starting the bot,
> or set `PROXY_URL` in `.env`.

## Run it manually

```bash
python -m venv venv
# Windows: venv\Scripts\activate    |  Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then paste your keys into .env
python bot.py
```

## Run it 24/7 on a server

Only run **one** copy of the bot per token (close `start.bat` on your PC once the server is running),
otherwise Telegram rejects the second copy. A server outside your country also means no VPN is needed.

### Option A – Railway (easiest, everything in the browser)

1. Merge the pull request so the code is on `main`.
2. Go to https://railway.com → **Login with GitHub**.
3. **New Project → Deploy from GitHub repo →** `hunain757/gametelegram`. Railway finds the `Dockerfile`
   and `railway.json` (auto-restart) by itself.
4. Open the service → **Variables** → add `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `TWELVEDATA_API_KEY`
   (and optionally `ADMIN_IDS`, `CHANNEL_ID`, any setting from `.env.example`).
5. **Settings → Volumes → Add volume**, mount path **`/data`** (keeps users, trades and stats across restarts).
6. **Settings → Region**: pick a Europe or Asia region (Binance volume data is not available from US servers).
7. **Deploy**. The **Logs** tab should show `Gold SMC AI bot started`.

Every push to `main` redeploys automatically. Railway is a paid service after its trial (a bot this size
fits the smallest plan); check their current pricing.

### Option B – Any Ubuntu / Debian VPS (cheapest long-term)

Rent a small Linux VPS (1 GB RAM is plenty; Hetzner, Contabo, DigitalOcean, Vultr, or Oracle Cloud's
free tier), open its console / SSH and run:

```bash
curl -fsSL https://raw.githubusercontent.com/hunain757/gametelegram/main/deploy/setup_vps.sh | sudo bash
```

It installs everything, asks for your keys once, and runs the bot as a `goldbot` service that restarts
on crashes and reboots. Run the same command again to update to the latest code.

```bash
sudo journalctl -u goldbot -f        # live logs
sudo systemctl restart goldbot       # restart
sudo nano /opt/goldbot/.env          # change keys/settings (then restart)
```

Gold is closed on weekends (Fri ~21:00 → Sun ~22:00 UTC), so the bot does not scan then.

## Free-plan limits (handled by the bot)

- **Twelve Data**: 800 requests/day, 8/min. Higher timeframes are cached, so a 5-minute scan uses ~450/day.
- **Gemini**: ~5 requests/minute *per model*. Each agent uses a different model, busy or overloaded models
  are skipped automatically, and the AI desk runs only when the engine has found a real setup.

## Settings

All optional settings (styles, scan interval, score / confidence / vote thresholds, R:R, models,
channel, admins, proxy) are documented in `.env.example`.

## Tests

```bash
python -m unittest discover -s tests -t .
```

---
⚠️ These signals are not financial advice. No bot or AI can guarantee profits or catch every move.
Always use a stop loss and risk only a small part of your account per trade.
