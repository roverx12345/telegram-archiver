# Linux Deployment Guide

This guide assumes Ubuntu or Debian and deploys the bot with `systemd`.

## 1. Install prerequisites

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

## 2. Bootstrap the project

```bash
git clone https://github.com/roverx12345/telegram-forward-archiver-bot.git /opt/telegram-forward-archiver-bot
cd /opt/telegram-forward-archiver-bot
bash deploy/linux/install.sh
```

## 3. Configure the bot

Edit `/opt/telegram-forward-archiver-bot/.env` and set at least:

```env
BOT_TOKEN=...
PAIR_CODE=...
DOWNLOAD_DIR=./downloads
DB_PATH=./data/bot.db
MAX_DOWNLOAD_RETRIES=3
```

## 4. Validate a manual start

```bash
cd /opt/telegram-forward-archiver-bot
source .venv/bin/activate
tele-bot
```

## 5. Install the service

Copy and adjust the template:

```bash
sudo cp deploy/systemd/tele-bot.service /etc/systemd/system/tele-bot.service
sudo nano /etc/systemd/system/tele-bot.service
```

Update:

- `User=telebot`
- `WorkingDirectory=/opt/telegram-forward-archiver-bot`
- `ExecStart=/opt/telegram-forward-archiver-bot/.venv/bin/tele-bot`

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tele-bot
sudo systemctl status tele-bot
journalctl -u tele-bot -f
```
