# Windows Setup Guide

## 1. Install dependencies

- Python 3.10 or newer
- Git

## 2. Clone and install

```powershell
git clone https://github.com/roverx12345/telegram-archiver.git
cd telegram-archiver
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

## 3. Configure `.env`

```env
BOT_TOKEN=...
PAIR_CODE=...
DOWNLOAD_DIR=./downloads
DB_PATH=./data/bot.db
LOCAL_BOT_API_URL=
LOG_LEVEL=INFO
MAX_DOWNLOAD_RETRIES=3
```

## 4. Start the bot

```powershell
.venv\Scripts\telegram-archiver.exe bot
```

If PowerShell blocks activation scripts:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
