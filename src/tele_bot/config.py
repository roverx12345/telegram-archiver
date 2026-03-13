from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    pair_code: str | None
    download_dir: Path
    db_path: Path
    local_bot_api_url: str | None
    log_level: str
    allow_unpaired_private: bool


def load_settings() -> Settings:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required")

    download_dir = Path(os.getenv("DOWNLOAD_DIR", "./downloads")).expanduser().resolve()
    db_path = Path(os.getenv("DB_PATH", "./data/bot.db")).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    pair_code = os.getenv("PAIR_CODE", "").strip() or None
    local_bot_api_url = os.getenv("LOCAL_BOT_API_URL", "").strip() or None

    return Settings(
        bot_token=bot_token,
        pair_code=pair_code,
        download_dir=download_dir,
        db_path=db_path,
        local_bot_api_url=local_bot_api_url,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        allow_unpaired_private=_to_bool(os.getenv("ALLOW_UNPAIRED_PRIVATE"), default=False),
    )
