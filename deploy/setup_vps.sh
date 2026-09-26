#!/usr/bin/env bash
# One-command install of the Gold AI bot on an Ubuntu/Debian server (runs 24/7, restarts on crash/reboot).
#
#   curl -fsSL https://raw.githubusercontent.com/hunain757/gametelegram/main/deploy/setup_vps.sh | sudo bash
#
# Run it again at any time to update the bot to the latest code.
set -euo pipefail

REPO="${REPO:-https://github.com/hunain757/gametelegram.git}"
BRANCH="${BRANCH:-main}"
DIR=/opt/goldbot
SERVICE=goldbot

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (use sudo)." >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git >/dev/null

echo "==> Getting the bot code ($BRANCH)"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q "$BRANCH"
  git -C "$DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi

echo "==> Installing Python packages"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

if ! id "$SERVICE" >/dev/null 2>&1; then
  useradd --system --home "$DIR" --shell /usr/sbin/nologin "$SERVICE"
fi

if [ ! -f "$DIR/.env" ]; then
  echo
  echo "==> First run: paste your keys (they stay on this server only)"
  read -r -p "Telegram bot token: " TG < /dev/tty
  read -r -p "Gemini API key: " GM < /dev/tty
  read -r -p "Twelve Data API key: " TD < /dev/tty
  read -r -p "Your Telegram user id for admin (optional, Enter to skip): " ADMIN < /dev/tty
  {
    echo "TELEGRAM_BOT_TOKEN=$TG"
    echo "GEMINI_API_KEY=$GM"
    echo "TWELVEDATA_API_KEY=$TD"
    echo "ADMIN_IDS=$ADMIN"
  } > "$DIR/.env"
fi
chown -R "$SERVICE:$SERVICE" "$DIR"
chmod 600 "$DIR/.env"

echo "==> Creating the $SERVICE service"
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=Gold SMC AI Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python bot.py
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"
sleep 5
systemctl --no-pager --lines=5 status "$SERVICE" || true

echo
echo "✅ Bot is running 24/7. Useful commands:"
echo "   Live logs:  sudo journalctl -u $SERVICE -f"
echo "   Restart:    sudo systemctl restart $SERVICE"
echo "   Stop:       sudo systemctl stop $SERVICE"
echo "   Edit keys:  sudo nano $DIR/.env   (then restart)"
