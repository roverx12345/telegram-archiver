# Linux Deployment Guide

This guide assumes Ubuntu or Debian and deploys the forward-to-bot source with `systemd`.

## 1. Install prerequisites

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

## 2. Bootstrap the project

```bash
git clone https://github.com/roverx12345/telegram-archiver.git /opt/telegram-archiver
cd /opt/telegram-archiver
bash deploy/linux/install.sh
```

## 3. Configure the bot

Edit `/opt/telegram-archiver/.env` and set at least:

```env
BOT_TOKEN=...
PAIR_CODE=...
DOWNLOAD_DIR=./downloads
DB_PATH=./data/bot.db
MAX_DOWNLOAD_RETRIES=3
```

## 4. Validate a manual start

```bash
cd /opt/telegram-archiver
source .venv/bin/activate
telegram-archiver bot
```

## 5. Install the service

Copy and adjust the template:

```bash
sudo cp deploy/systemd/telegram-archiver-bot.service /etc/systemd/system/telegram-archiver-bot.service
sudo nano /etc/systemd/system/telegram-archiver-bot.service
```

Update:

- `User=telebot`
- `WorkingDirectory=/opt/telegram-archiver`
- `ExecStart=/opt/telegram-archiver/.venv/bin/telegram-archiver bot`

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-archiver-bot
sudo systemctl status telegram-archiver-bot
journalctl -u telegram-archiver-bot -f
```
