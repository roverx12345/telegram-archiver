# Docker Setup Guide

Use Docker Compose if you also want to run a local Telegram Bot API server.

## 1. Prepare `.env`

```env
BOT_TOKEN=...
PAIR_CODE=...
DOWNLOAD_DIR=./downloads
DB_PATH=./data/bot.db
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
LOCAL_BOT_API_URL=http://telegram-bot-api:8081
MAX_DOWNLOAD_RETRIES=3
```

## 2. Start services

```bash
docker compose up -d --build
```

## 3. Check logs

```bash
docker compose logs -f bot
docker compose logs -f telegram-bot-api
```
