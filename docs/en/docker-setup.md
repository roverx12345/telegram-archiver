# Docker Setup Guide

Docker Compose runs the archiver as one application with two source services:

- `bot`: forward-to-bot media archiving.
- `saved-archiver`: Saved Messages archiving.

There are also two Docker modes:

- Base mode: official cloud Bot API.
- Large-files mode: local Telegram Bot API server for files above the cloud Bot API limit.

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

If you want the Saved Messages source or large-file support, also add:

```env
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_SESSION=./data/saved_messages.session
SAVED_ARCHIVE_EXISTING=true
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
docker compose logs -f saved-archiver
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
