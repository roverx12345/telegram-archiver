# Docker Setup Guide

There are now two Docker modes:

- Base mode: bot only, suitable when you do not need large-file downloads.
- Large-files mode: bot + local Telegram Bot API server, suitable for files above the cloud Bot API limit.

Files:

- Base compose: `docker-compose.yml`
- Large-files override: `docker-compose.large-files.yml`

## 1. Prepare `.env`

```env
BOT_TOKEN=...
PAIR_CODE=...
DOWNLOAD_DIR=./downloads
DB_PATH=./data/bot.db
MAX_DOWNLOAD_RETRIES=3
```

If you want large-file support, also add:

```env
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
```

## 2. Start services

Base mode:

```bash
docker compose up -d --build
```

Large-files mode:

```bash
docker compose -f docker-compose.yml -f docker-compose.large-files.yml up -d --build
```

## 3. Check logs

```bash
docker compose logs -f bot
```

If you use large-files mode:

```bash
docker compose -f docker-compose.yml -f docker-compose.large-files.yml logs -f bot
docker compose -f docker-compose.yml -f docker-compose.large-files.yml logs -f telegram-bot-api
```

## 4. Notes

- The bot container now waits for `LOCAL_BOT_API_URL` before starting the polling loop.
- The large-files override adds a healthcheck for `telegram-bot-api` and only starts the bot after that service is healthy.
- For files larger than the official cloud Bot API download limit, use the large-files mode.
