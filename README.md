# Telegram Archiver

Archive Telegram media to local storage or NAS with dedupe, retry, and restart recovery.

This is one archiver application with two source adapters:

- `bot`: receives forwarded media in a private chat with a Telegram bot.
- `saved`: scans and listens to your own Saved Messages with Telethon.

[Read the Chinese documentation](./README.zh-CN.md)

## Features

- Downloads forwarded media sent to the bot in private chat.
- Archives existing and new Saved Messages media.
- Resumes interrupted Saved Messages media downloads from `.part` files.
- Deduplicates by Telegram file identity first, then by downloaded file `sha256`.
- Stores shared metadata in one SQLite database.
- Persists bot download jobs and retries them after failures or restarts.
- Preserves pending Telegram bot updates on restart.
- Includes bot job inspection commands: `/status`, `/jobs`, `/failed`, `/retry_failed`.
- Supports a local Telegram Bot API server for faster bot downloads.

## Quick Start

```bash
git clone https://github.com/roverx12345/telegram-archiver.git
cd telegram-archiver
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
telegram-archiver bot
```

Set at least the following in `.env`:

```env
BOT_TOKEN=...
PAIR_CODE=...
DOWNLOAD_DIR=./downloads
DB_PATH=./data/bot.db
MAX_DOWNLOAD_RETRIES=3
```

For the Saved Messages source, also set:

```env
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_SESSION=./data/saved_messages.session
SAVED_ARCHIVE_EXISTING=true
```

## Run Modes

```bash
telegram-archiver bot
telegram-archiver saved
telegram-archiver saved-stats
telegram-archiver all
```

Saved Messages downloads keep resumable partial files in:

```text
DOWNLOAD_DIR/.tmp/*.part
```

The bot source still retries failed downloads from the beginning.

If Docker `saved-archiver` is already running, stop it before `saved-stats` because Telethon uses the same session file:

```bash
docker compose stop saved-archiver
docker compose run --rm saved-archiver telegram-archiver saved-stats
# quick sample:
docker compose run --rm saved-archiver telegram-archiver saved-stats --limit 1000
docker compose start saved-archiver
```

## Detailed Guides

- [Linux deployment with systemd](./docs/en/linux-deployment.md)
- [Windows setup](./docs/en/windows-setup.md)
- [Docker setup](./docs/en/docker-setup.md)
- [Operations guide](./docs/en/operations.md)
- [Change log](./CHANGELOG.md)

## Included Templates

- [systemd service template](./deploy/systemd/telegram-archiver-bot.service)
- [Linux bootstrap script](./deploy/linux/install.sh)
- [GitHub Actions CI workflow](./.github/workflows/ci.yml)
- [Base Docker Compose](./docker-compose.yml)
- [Large-files Docker override](./docker-compose.large-files.yml)

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
python -m compileall src tests
```

## License

[MIT](./LICENSE)
