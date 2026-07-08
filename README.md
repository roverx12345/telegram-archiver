# Telegram Archiver

Archive Telegram media to local storage or NAS with dedupe, retry, and restart recovery.

This is one archiver application with multiple source adapters:

- `bot`: receives forwarded media in a private chat with a Telegram bot.
- `saved`: scans and listens to your own Saved Messages with Telethon.
- `channels`: scans and listens to configured joined channels with Telethon.

[Read the Chinese documentation](./README.zh-CN.md)

## Features

- Downloads forwarded media sent to the bot in private chat.
- Archives existing and new Saved Messages media.
- Archives existing and new media from configured channels.
- Resumes interrupted Saved Messages and channel media downloads from `.part` files.
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
SAVED_RETRY_PARTIALS_ON_START=true
SAVED_RETRY_PARTIALS_LIMIT=0
SAVED_RECENT_SCAN_INTERVAL_SECONDS=900
SAVED_RECENT_SCAN_LIMIT=2000
```

For the channel source, also set:

```env
CHANNEL_ARCHIVE_PEERS=-1002683725559,@public_channel
CHANNEL_TELEGRAM_SESSION=./data/channel_archiver.session
CHANNEL_ARCHIVE_EXISTING=true
CHANNEL_RETRY_PARTIALS_ON_START=true
CHANNEL_RECENT_SCAN_INTERVAL_SECONDS=900
CHANNEL_RECENT_SCAN_LIMIT=2000
```

## Run Modes

```bash
telegram-archiver bot
telegram-archiver saved
telegram-archiver channels
telegram-archiver saved-stats
telegram-archiver channels-list
telegram-archiver channel-check -1002683725559 --limit 20 --download-sample
telegram-archiver clean-tmp
telegram-archiver all
```

Saved Messages downloads keep resumable partial files in:

```text
DOWNLOAD_DIR/.tmp/*.part
```

The bot source still retries failed downloads from the beginning.

On startup, the Saved Messages source can revisit message IDs parsed from existing `.tmp/saved_<message_id>_*.part` files and resume those partial downloads. The channel source does the same for `.tmp/channel_<channel_id>_<message_id>_*.part` files. Both Telethon sources can periodically rescan recent messages so missed realtime updates are picked up later.

Stale partial files can be reviewed and cleaned with:

```bash
telegram-archiver clean-tmp --older-than-days 30
telegram-archiver clean-tmp --older-than-days 30 --delete
telegram-archiver clean-tmp --download-dir /srv/nas/nasdata/telegram-forward-archiver-bot/downloads --older-than-days 30
```

Without `--delete`, `clean-tmp` only prints a dry-run summary. Stop active download services before deleting stale partial files.

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
