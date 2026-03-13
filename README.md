# Telegram Forward Archiver Bot

Telegram bot for forwarded media archiving with dedupe, retry, and restart recovery.

[Read the Chinese documentation](./README.zh-CN.md)

## Features

- Automatically downloads forwarded media sent to the bot in private chat.
- Deduplicates by Telegram `file_unique_id` first, then by downloaded file `sha256`.
- Supports pairing-based authorization with `/pair <code>`.
- Persists download jobs and retries them after failures or restarts.
- Preserves pending Telegram updates on restart.
- Includes job inspection commands: `/status`, `/jobs`, `/failed`, `/retry_failed`.
- Supports a local Telegram Bot API server for faster downloads.

## Quick Start

```bash
git clone https://github.com/roverx12345/telegram-forward-archiver-bot.git
cd telegram-forward-archiver-bot
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
tele-bot
```

Set at least the following in `.env`:

```env
BOT_TOKEN=...
PAIR_CODE=...
DOWNLOAD_DIR=./downloads
DB_PATH=./data/bot.db
MAX_DOWNLOAD_RETRIES=3
```

## Detailed Guides

- [Linux deployment with systemd](./docs/en/linux-deployment.md)
- [Windows setup](./docs/en/windows-setup.md)
- [Docker setup](./docs/en/docker-setup.md)
- [Operations guide](./docs/en/operations.md)
- [Change log](./CHANGELOG.md)

## Included Templates

- [systemd service template](./deploy/systemd/tele-bot.service)
- [Linux bootstrap script](./deploy/linux/install.sh)
- [GitHub Actions CI workflow](./.github/workflows/ci.yml)

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
