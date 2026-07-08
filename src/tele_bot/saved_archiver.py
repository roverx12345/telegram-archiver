from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from .db import Database
from .media import (
    MediaRef,
    build_storage_name,
    media_storage_dir,
    sanitize_filename,
    sha256sum,
    unique_target_path,
)

LOGGER = logging.getLogger(__name__)
SAVED_PARTIAL_NAME_RE = re.compile(r"^saved_(\d+)_.*\.part$")
CHANNEL_PARTIAL_NAME_RE = re.compile(r"^channel_(-?\d+)_(\d+)_.*\.part$")
DEFAULT_ARCHIVE_EXTENSIONS = frozenset({
    ".7z",
    ".001",
    ".bz2",
    ".gz",
    ".rar",
    ".tar",
    ".tar.bz2",
    ".tar.gz",
    ".tar.xz",
    ".tbz",
    ".tgz",
    ".txz",
    ".xz",
    ".zip",
    ".zst",
})

@dataclass(frozen=True)
class SavedArchiverSettings:
    api_id: int
    api_hash: str
    session_path: Path
    download_dir: Path
    text_dir: Path
    db_path: Path
    log_level: str
    archive_existing: bool
    scan_progress_every: int
    recent_scan_interval_seconds: int
    recent_scan_limit: int
    retry_partials_on_start: bool
    retry_partials_limit: int
    blocked_forward_chat_ids: frozenset[int]
    blocked_forward_keywords: frozenset[str]
    proxy: tuple | None

@dataclass(frozen=True)
class TelethonSessionSettings:
    api_id: int
    api_hash: str
    session_path: Path
    log_level: str
    proxy: tuple | None

@dataclass(frozen=True)
class SavedStats:
    scanned_messages: int = 0
    media_messages: int = 0
    blocked_media: int = 0
    already_archived_by_unique_id: int = 0
    already_archived_by_file_id: int = 0
    download_candidates: int = 0

@dataclass(frozen=True)
class ArchiveErrorClassification:
    kind: str
    retryable: bool
    message: str

@dataclass(frozen=True)
class MultipartArchivePart:
    group_key: str
    first_name: str
    order: int
    is_first: bool
    requires_sibling: bool = False

@dataclass(frozen=True)
class ProcessedArchive:
    path: Path
    ref: MediaRef
    cleanup_paths: tuple[Path, ...] = ()
    status: str = "plain"
    password_matched: bool = False
    original_name: str | None = None
    output_name: str | None = None
    part_group: str | None = None
    part_count: int | None = None
    error_message: str | None = None

@dataclass(frozen=True)
class ChannelInfo:
    id: int | None
    title: str
    username: str | None
    broadcast: bool
    megagroup: bool
    protected_content: bool

@dataclass(frozen=True)
class ChannelCheckResult:
    peer: str
    id: int | None
    title: str
    username: str | None
    protected_content: bool
    scanned_messages: int
    media_messages: int
    protected_messages: int
    sample_download_status: str
    sample_download_detail: str

@dataclass(frozen=True)
class ChannelTargetSettings:
    peer: str
    allowed_extensions: frozenset[str]
    media_types: frozenset[str]
    archive_text: bool
    try_protected_content: bool
    download_dir: Path | None
    text_dir: Path | None
    archive_existing: bool
    existing_scan_limit: int
    recent_scan_limit: int
    start_date: datetime | None
    end_date: datetime | None
    download_delay_seconds: float

@dataclass(frozen=True)
class ChannelArchiverSettings:
    archive: SavedArchiverSettings
    targets: tuple[ChannelTargetSettings, ...]
    password_file: Path | None
    strip_archive_passwords: bool

    @property
    def peers(self) -> tuple[str, ...]:
        return tuple(target.peer for target in self.targets)

def load_saved_archiver_settings() -> SavedArchiverSettings:
    load_dotenv()

    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id_raw or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")

    download_dir = Path(os.getenv("DOWNLOAD_DIR", "./downloads")).expanduser().resolve()
    text_dir = Path(os.getenv("TEXT_DOWNLOAD_DIR", str(download_dir / "texts"))).expanduser().resolve()
    db_path = Path(os.getenv("DB_PATH", "./data/bot.db")).expanduser().resolve()
    session_path = Path(os.getenv("TELEGRAM_SESSION", "./data/saved_messages.session")).expanduser().resolve()

    download_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    return SavedArchiverSettings(
        api_id=int(api_id_raw),
        api_hash=api_hash,
        session_path=session_path,
        download_dir=download_dir,
        text_dir=text_dir,
        db_path=db_path,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        archive_existing=_to_bool(os.getenv("SAVED_ARCHIVE_EXISTING"), default=True),
        scan_progress_every=max(0, int(os.getenv("SAVED_SCAN_PROGRESS_EVERY", "1000"))),
        recent_scan_interval_seconds=max(0, int(os.getenv("SAVED_RECENT_SCAN_INTERVAL_SECONDS", "900"))),
        recent_scan_limit=max(0, int(os.getenv("SAVED_RECENT_SCAN_LIMIT", "2000"))),
        retry_partials_on_start=_to_bool(os.getenv("SAVED_RETRY_PARTIALS_ON_START"), default=True),
        retry_partials_limit=max(0, int(os.getenv("SAVED_RETRY_PARTIALS_LIMIT", "0"))),
        blocked_forward_chat_ids=parse_int_set(os.getenv("SAVED_BLOCKED_FORWARD_CHAT_IDS")),
        blocked_forward_keywords=parse_keyword_set(os.getenv("SAVED_BLOCKED_FORWARD_KEYWORDS")),
        proxy=parse_telethon_proxy(os.getenv("TELETHON_PROXY")),
    )


def load_telethon_session_settings() -> TelethonSessionSettings:
    load_dotenv()

    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id_raw or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")

    session_path = Path(os.getenv("TELEGRAM_SESSION", "./data/saved_messages.session")).expanduser().resolve()
    session_path.parent.mkdir(parents=True, exist_ok=True)

    return TelethonSessionSettings(
        api_id=int(api_id_raw),
        api_hash=api_hash,
        session_path=session_path,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        proxy=parse_telethon_proxy(os.getenv("TELETHON_PROXY")),
    )


def load_channel_archiver_settings() -> ChannelArchiverSettings:
    load_dotenv()

    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id_raw or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")

    download_dir = Path(os.getenv("DOWNLOAD_DIR", "./downloads")).expanduser().resolve()
    text_dir = Path(os.getenv("TEXT_DOWNLOAD_DIR", str(download_dir / "texts"))).expanduser().resolve()
    db_path = Path(os.getenv("DB_PATH", "./data/bot.db")).expanduser().resolve()
    session_default = os.getenv("TELEGRAM_SESSION", "./data/saved_messages.session")
    session_path = Path(os.getenv("CHANNEL_TELEGRAM_SESSION", "./data/channel_archiver.session") or session_default).expanduser().resolve()

    download_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    archive = SavedArchiverSettings(
        api_id=int(api_id_raw),
        api_hash=api_hash,
        session_path=session_path,
        download_dir=download_dir,
        text_dir=text_dir,
        db_path=db_path,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        archive_existing=_to_bool(os.getenv("CHANNEL_ARCHIVE_EXISTING"), default=True),
        scan_progress_every=max(0, int(os.getenv("CHANNEL_SCAN_PROGRESS_EVERY", os.getenv("SAVED_SCAN_PROGRESS_EVERY", "1000")))),
        recent_scan_interval_seconds=max(0, int(os.getenv("CHANNEL_RECENT_SCAN_INTERVAL_SECONDS", "900"))),
        recent_scan_limit=max(0, int(os.getenv("CHANNEL_RECENT_SCAN_LIMIT", "2000"))),
        retry_partials_on_start=_to_bool(os.getenv("CHANNEL_RETRY_PARTIALS_ON_START"), default=True),
        retry_partials_limit=max(0, int(os.getenv("CHANNEL_RETRY_PARTIALS_LIMIT", "0"))),
        blocked_forward_chat_ids=frozenset(),
        blocked_forward_keywords=frozenset(),
        proxy=parse_telethon_proxy(os.getenv("TELETHON_PROXY")),
    )
    password_file_raw = os.getenv("CHANNEL_ARCHIVE_PASSWORD_FILE", "").strip()
    password_file = Path(password_file_raw).expanduser().resolve() if password_file_raw else None
    default_target = ChannelTargetSettings(
        peer="",
        allowed_extensions=parse_extension_set(os.getenv("CHANNEL_ARCHIVE_EXTENSIONS")),
        media_types=parse_keyword_set(os.getenv("CHANNEL_ARCHIVE_MEDIA_TYPES")),
        archive_text=_to_bool(os.getenv("CHANNEL_ARCHIVE_TEXT"), default=True),
        try_protected_content=_to_bool(os.getenv("CHANNEL_TRY_PROTECTED_CONTENT"), default=False),
        download_dir=None,
        text_dir=None,
        archive_existing=archive.archive_existing,
        existing_scan_limit=max(0, int(os.getenv("CHANNEL_EXISTING_SCAN_LIMIT", "0"))),
        recent_scan_limit=archive.recent_scan_limit,
        start_date=parse_config_datetime(os.getenv("CHANNEL_START_DATE"), end_of_day=False),
        end_date=parse_config_datetime(os.getenv("CHANNEL_END_DATE"), end_of_day=True),
        download_delay_seconds=max(0.0, float(os.getenv("CHANNEL_DOWNLOAD_DELAY_SECONDS", "0"))),
    )
    config_path_raw = os.getenv("CHANNEL_ARCHIVE_CONFIG", "").strip()
    if config_path_raw:
        targets = load_channel_target_config(Path(config_path_raw).expanduser().resolve(), default_target)
    else:
        peers = parse_peer_list(os.getenv("CHANNEL_ARCHIVE_PEERS"))
        if not peers:
            raise RuntimeError("CHANNEL_ARCHIVE_PEERS or CHANNEL_ARCHIVE_CONFIG is required for telegram-archiver channels")
        targets = tuple(channel_target_with_peer(default_target, peer) for peer in peers)

    return ChannelArchiverSettings(
        archive=archive,
        targets=targets,
        password_file=password_file,
        strip_archive_passwords=_to_bool(os.getenv("CHANNEL_STRIP_ARCHIVE_PASSWORDS"), default=False),
    )


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def parse_int_set(value: str | None) -> frozenset[int]:
    if not value or not value.strip():
        return frozenset()
    ids: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            ids.add(int(item))
    return frozenset(ids)

def parse_keyword_set(value: str | None) -> frozenset[str]:
    if not value or not value.strip():
        return frozenset()
    return frozenset(item.strip().casefold() for item in value.split(",") if item.strip())

def parse_peer_list(value: str | None) -> tuple[str, ...]:
    if not value or not value.strip():
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())

def parse_extension_set(value: str | None) -> frozenset[str]:
    if not value or not value.strip():
        return frozenset()
    extensions: set[str] = set()
    for item in value.split(","):
        item = item.strip().casefold()
        if not item:
            continue
        if item == "archives":
            extensions.update(DEFAULT_ARCHIVE_EXTENSIONS)
            continue
        if not item.startswith("."):
            item = f".{item}"
        extensions.add(item)
    return frozenset(extensions)


def parse_config_extension_set(value: object, default: frozenset[str]) -> frozenset[str]:
    if value is None:
        return default
    if isinstance(value, str):
        return parse_extension_set(value)
    if isinstance(value, list):
        return parse_extension_set(",".join(str(item) for item in value))
    raise RuntimeError("channel config extensions must be a string or list")


def parse_config_keyword_set(value: object, default: frozenset[str]) -> frozenset[str]:
    if value is None:
        return default
    if isinstance(value, str):
        return parse_keyword_set(value)
    if isinstance(value, list):
        return frozenset(str(item).strip().casefold() for item in value if str(item).strip())
    raise RuntimeError("channel config media_types must be a string or list")


def parse_config_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _to_bool(value, default=default)
    raise RuntimeError("channel config boolean values must be bool or string")


def parse_config_int(value: object, default: int) -> int:
    if value is None:
        return default
    return max(0, int(value))


def parse_config_float(value: object, default: float) -> float:
    if value is None:
        return default
    return max(0.0, float(value))


def parse_config_path(value: object) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    return Path(str(value)).expanduser().resolve()


def parse_config_datetime(value: object, *, end_of_day: bool) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed_date = datetime.fromisoformat(text).date()
        parsed = datetime.combine(parsed_date, time.max if end_of_day else time.min, tzinfo=timezone.utc)
    else:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def channel_target_with_peer(target: ChannelTargetSettings, peer: str) -> ChannelTargetSettings:
    return ChannelTargetSettings(
        peer=peer,
        allowed_extensions=target.allowed_extensions,
        media_types=target.media_types,
        archive_text=target.archive_text,
        try_protected_content=target.try_protected_content,
        download_dir=target.download_dir,
        text_dir=target.text_dir,
        archive_existing=target.archive_existing,
        existing_scan_limit=target.existing_scan_limit,
        recent_scan_limit=target.recent_scan_limit,
        start_date=target.start_date,
        end_date=target.end_date,
        download_delay_seconds=target.download_delay_seconds,
    )


def load_channel_target_config(path: Path, default: ChannelTargetSettings) -> tuple[ChannelTargetSettings, ...]:
    if not path.exists():
        raise RuntimeError(f"CHANNEL_ARCHIVE_CONFIG does not exist: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        raise RuntimeError("CHANNEL_ARCHIVE_CONFIG must be a JSON object with a channels list")

    targets: list[ChannelTargetSettings] = []
    for item in data["channels"]:
        if not isinstance(item, dict):
            raise RuntimeError("each channel config entry must be an object")
        peer = str(item.get("peer", "")).strip()
        if not peer:
            raise RuntimeError("each channel config entry requires peer")
        targets.append(
            ChannelTargetSettings(
                peer=peer,
                allowed_extensions=parse_config_extension_set(item.get("extensions"), default.allowed_extensions),
                media_types=parse_config_keyword_set(item.get("media_types"), default.media_types),
                archive_text=parse_config_bool(item.get("archive_text"), default.archive_text),
                try_protected_content=parse_config_bool(item.get("try_protected_content"), default.try_protected_content),
                download_dir=parse_config_path(item.get("download_dir")) or default.download_dir,
                text_dir=parse_config_path(item.get("text_dir")) or default.text_dir,
                archive_existing=parse_config_bool(item.get("archive_existing"), default.archive_existing),
                existing_scan_limit=parse_config_int(item.get("existing_limit"), default.existing_scan_limit),
                recent_scan_limit=parse_config_int(item.get("recent_limit"), default.recent_scan_limit),
                start_date=parse_config_datetime(item.get("start_date"), end_of_day=False) or default.start_date,
                end_date=parse_config_datetime(item.get("end_date"), end_of_day=True) or default.end_date,
                download_delay_seconds=parse_config_float(item.get("download_delay_seconds"), default.download_delay_seconds),
            )
        )
    if not targets:
        raise RuntimeError("CHANNEL_ARCHIVE_CONFIG channels list must not be empty")
    return tuple(targets)


def parse_telethon_proxy(value: str | None) -> tuple | None:
    if not value or not value.strip():
        return None

    try:
        import python_socks
    except ImportError as exc:
        raise RuntimeError("TELETHON_PROXY requires the python-socks package") from exc

    parsed = urlparse(value.strip())
    proxy_types = {
        "socks5": python_socks.ProxyType.SOCKS5,
        "socks5h": python_socks.ProxyType.SOCKS5,
        "socks4": python_socks.ProxyType.SOCKS4,
        "http": python_socks.ProxyType.HTTP,
        "https": python_socks.ProxyType.HTTP,
    }
    proxy_type = proxy_types.get(parsed.scheme.lower())
    if proxy_type is None:
        raise RuntimeError("TELETHON_PROXY must use socks5h://, socks5://, socks4://, http://, or https://")
    if not parsed.hostname or parsed.port is None:
        raise RuntimeError("TELETHON_PROXY must include host and port")

    rdns = parsed.scheme.lower() == "socks5h"
    return (proxy_type, parsed.hostname, parsed.port, rdns, parsed.username, parsed.password)

async def scan_existing_saved_messages(
    client: TelegramClient,
    database: Database,
    settings: SavedArchiverSettings,
    *,
    archive_one: Callable[[Message], Awaitable[None]] | None = None,
) -> None:
    LOGGER.warning("Scanning existing Saved Messages media")
    scanned = 0
    async for message in client.iter_messages("me", reverse=True):
        scanned += 1
        await archive_saved_message(client, database, settings, message, archive_one=archive_one)
        if settings.scan_progress_every > 0 and scanned % settings.scan_progress_every == 0:
            LOGGER.warning("Saved Messages scan progress: scanned=%s", scanned)
    LOGGER.warning("Existing Saved Messages scan finished: scanned=%s", scanned)


async def scan_recent_saved_messages(
    client: TelegramClient,
    database: Database,
    settings: SavedArchiverSettings,
    *,
    limit: int,
    archive_one: Callable[[Message], Awaitable[None]] | None = None,
) -> None:
    if limit <= 0:
        return

    LOGGER.warning("Scanning recent Saved Messages media: limit=%s", limit)
    scanned = 0
    async for message in client.iter_messages("me", limit=limit):
        scanned += 1
        await archive_saved_message(client, database, settings, message, archive_one=archive_one)
        if settings.scan_progress_every > 0 and scanned % settings.scan_progress_every == 0:
            LOGGER.warning("Recent Saved Messages scan progress: scanned=%s limit=%s", scanned, limit)
    LOGGER.warning("Recent Saved Messages scan finished: scanned=%s limit=%s", scanned, limit)


async def retry_partial_saved_messages(
    client: TelegramClient,
    database: Database,
    settings: SavedArchiverSettings,
    *,
    archive_one: Callable[[Message], Awaitable[None]] | None = None,
) -> None:
    message_ids = saved_partial_message_ids(settings.download_dir / ".tmp", limit=settings.retry_partials_limit)
    if not message_ids:
        LOGGER.warning("No Saved Messages partial downloads found to retry")
        return

    LOGGER.warning("Retrying Saved Messages partial downloads: count=%s", len(message_ids))
    scanned = 0
    for index in range(0, len(message_ids), 100):
        batch = message_ids[index:index + 100]
        messages = await client.get_messages("me", ids=batch)
        if not isinstance(messages, list):
            messages = [messages]
        for message in messages:
            if message is None:
                continue
            scanned += 1
            await archive_saved_message(client, database, settings, message, archive_one=archive_one)
            if settings.scan_progress_every > 0 and scanned % settings.scan_progress_every == 0:
                LOGGER.warning("Saved Messages partial retry progress: scanned=%s total=%s", scanned, len(message_ids))
    LOGGER.warning("Saved Messages partial retry finished: scanned=%s total=%s", scanned, len(message_ids))


def saved_partial_message_ids(temp_dir: Path, *, limit: int = 0) -> list[int]:
    if not temp_dir.exists():
        return []

    partials: list[tuple[float, int]] = []
    for path in temp_dir.glob("saved_*.part"):
        match = SAVED_PARTIAL_NAME_RE.match(path.name)
        if match is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        partials.append((mtime, int(match.group(1))))

    seen: set[int] = set()
    message_ids: list[int] = []
    for _, message_id in sorted(partials):
        if message_id in seen:
            continue
        seen.add(message_id)
        message_ids.append(message_id)
        if limit > 0 and len(message_ids) >= limit:
            break
    return message_ids


async def archive_saved_message(
    client: TelegramClient,
    database: Database,
    settings: SavedArchiverSettings,
    message: Message,
    *,
    archive_one: Callable[[Message], Awaitable[None]] | None = None,
) -> None:
    if archive_one is not None:
        await archive_one(message)
    else:
        await archive_message(client, database, settings, message)


async def periodic_recent_scan_loop(
    client: TelegramClient,
    database: Database,
    settings: SavedArchiverSettings,
    *,
    archive_one: Callable[[Message], Awaitable[None]],
) -> None:
    while True:
        await asyncio.sleep(settings.recent_scan_interval_seconds)
        try:
            await scan_recent_saved_messages(
                client,
                database,
                settings,
                limit=settings.recent_scan_limit,
                archive_one=archive_one,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Periodic Saved Messages recent scan failed")


async def scan_existing_channel_messages(
    client: TelegramClient,
    database: Database,
    settings: SavedArchiverSettings,
    entity: object,
    *,
    archive_one: Callable[[Message], Awaitable[None]],
    limit: int = 0,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> None:
    info = channel_info_from_entity(entity)
    LOGGER.warning("Scanning existing channel media: peer=%s title=%s limit=%s", full_channel_peer_id(info.id), info.title, limit)
    scanned = 0
    async for message in client.iter_messages(entity, reverse=True):
        scanned += 1
        if message_in_date_range(message, start_date=start_date, end_date=end_date):
            await archive_one(message)
        if settings.scan_progress_every > 0 and scanned % settings.scan_progress_every == 0:
            LOGGER.warning("Channel scan progress: peer=%s scanned=%s", full_channel_peer_id(info.id), scanned)
        if limit > 0 and scanned >= limit:
            break
    LOGGER.warning("Existing channel scan finished: peer=%s scanned=%s", full_channel_peer_id(info.id), scanned)


async def scan_recent_channel_messages(
    client: TelegramClient,
    settings: SavedArchiverSettings,
    entity: object,
    *,
    limit: int,
    archive_one: Callable[[Message], Awaitable[None]],
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> None:
    if limit <= 0:
        return

    info = channel_info_from_entity(entity)
    LOGGER.warning("Scanning recent channel media: peer=%s limit=%s", full_channel_peer_id(info.id), limit)
    scanned = 0
    async for message in client.iter_messages(entity, limit=limit):
        scanned += 1
        if message_in_date_range(message, start_date=start_date, end_date=end_date):
            await archive_one(message)
        if settings.scan_progress_every > 0 and scanned % settings.scan_progress_every == 0:
            LOGGER.warning(
                "Recent channel scan progress: peer=%s scanned=%s limit=%s",
                full_channel_peer_id(info.id),
                scanned,
                limit,
            )
    LOGGER.warning("Recent channel scan finished: peer=%s scanned=%s limit=%s", full_channel_peer_id(info.id), scanned, limit)


async def retry_partial_channel_messages(
    client: TelegramClient,
    settings: SavedArchiverSettings,
    entities_by_id: dict[int, object],
    *,
    archive_one: Callable[[Message], Awaitable[None]],
) -> None:
    partials = channel_partial_message_refs(settings.download_dir / ".tmp", limit=settings.retry_partials_limit)
    if not partials:
        LOGGER.warning("No channel partial downloads found to retry")
        return

    LOGGER.warning("Retrying channel partial downloads: count=%s", len(partials))
    scanned = 0
    for channel_id, message_id in partials:
        entity = entities_by_id.get(channel_id)
        if entity is None:
            LOGGER.warning("Channel partial skipped because peer is not configured: channel_id=%s message=%s", channel_id, message_id)
            continue
        message = await client.get_messages(entity, ids=message_id)
        if message is None:
            continue
        scanned += 1
        await archive_one(message)
        if settings.scan_progress_every > 0 and scanned % settings.scan_progress_every == 0:
            LOGGER.warning("Channel partial retry progress: scanned=%s total=%s", scanned, len(partials))
    LOGGER.warning("Channel partial retry finished: scanned=%s total=%s", scanned, len(partials))


def channel_partial_message_refs(temp_dir: Path, *, limit: int = 0) -> list[tuple[int, int]]:
    if not temp_dir.exists():
        return []

    partials: list[tuple[float, int, int]] = []
    for path in temp_dir.glob("channel_*.part"):
        match = CHANNEL_PARTIAL_NAME_RE.match(path.name)
        if match is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        partials.append((mtime, int(match.group(1)), int(match.group(2))))

    seen: set[tuple[int, int]] = set()
    refs: list[tuple[int, int]] = []
    for _, channel_id, message_id in sorted(partials):
        ref = (channel_id, message_id)
        if ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        if limit > 0 and len(refs) >= limit:
            break
    return refs


async def periodic_channel_recent_scan_loop(
    client: TelegramClient,
    settings: SavedArchiverSettings,
    entities: list[object],
    targets_by_id: dict[int, ChannelTargetSettings],
    *,
    archive_one: Callable[[Message], Awaitable[None]],
) -> None:
    while True:
        await asyncio.sleep(settings.recent_scan_interval_seconds)
        for entity in entities:
            try:
                channel_id = normalized_channel_id(getattr(entity, "id", None))
                target = targets_by_id.get(channel_id)
                if target is None or target.recent_scan_limit <= 0:
                    continue
                await scan_recent_channel_messages(
                    client,
                    settings,
                    entity,
                    limit=target.recent_scan_limit,
                    archive_one=archive_one,
                    start_date=target.start_date,
                    end_date=target.end_date,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Periodic channel recent scan failed: peer=%s", full_channel_peer_id(getattr(entity, "id", None)))


def message_in_date_range(
    message: Message,
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> bool:
    message_date = getattr(message, "date", None)
    if message_date is None:
        return True
    if message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=timezone.utc)
    if start_date is not None and message_date < start_date:
        return False
    if end_date is not None and message_date > end_date:
        return False
    return True


async def run_archiver() -> None:
    settings = load_saved_archiver_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database = Database(settings.db_path)
    client = TelegramClient(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        proxy=settings.proxy,
    )

    try:
        async with client:
            me = await client.get_me()
            LOGGER.warning("Saved Messages archiver started for account id=%s", getattr(me, "id", "unknown"))

            archive_lock = asyncio.Lock()

            async def archive_one(message: Message) -> None:
                async with archive_lock:
                    await archive_message(client, database, settings, message)

            @client.on(events.NewMessage(chats="me"))
            async def handle_saved_message(event: events.NewMessage.Event) -> None:
                await archive_one(event.message)

            periodic_task: asyncio.Task[None] | None = None
            if settings.recent_scan_interval_seconds > 0 and settings.recent_scan_limit > 0:
                periodic_task = asyncio.create_task(
                    periodic_recent_scan_loop(client, database, settings, archive_one=archive_one)
                )
                LOGGER.warning(
                    "Periodic Saved Messages recent scan enabled: interval_seconds=%s limit=%s",
                    settings.recent_scan_interval_seconds,
                    settings.recent_scan_limit,
                )

            LOGGER.warning("Listening for new Saved Messages media")
            if settings.retry_partials_on_start:
                await retry_partial_saved_messages(client, database, settings, archive_one=archive_one)
            if settings.archive_existing:
                await scan_existing_saved_messages(client, database, settings, archive_one=archive_one)

            try:
                await client.run_until_disconnected()
            finally:
                if periodic_task is not None:
                    periodic_task.cancel()
                    try:
                        await periodic_task
                    except asyncio.CancelledError:
                        pass
    finally:
        database.close()


async def run_channel_archiver() -> None:
    channel_settings = load_channel_archiver_settings()
    settings = channel_settings.archive
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database = Database(settings.db_path)
    client = TelegramClient(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        proxy=settings.proxy,
    )

    try:
        async with client:
            me = await client.get_me()
            LOGGER.warning("Channel archiver started for account id=%s", getattr(me, "id", "unknown"))
            entities: list[object] = []
            targets_by_id: dict[int, ChannelTargetSettings] = {}
            for target in channel_settings.targets:
                entity = await resolve_channel_entity(client, target.peer)
                info = channel_info_from_entity(entity)
                if info.protected_content and not target.try_protected_content:
                    LOGGER.warning("Protected channel skipped: peer=%s title=%s", target.peer, info.title)
                    continue
                channel_id = normalized_channel_id(info.id)
                if channel_id == 0:
                    LOGGER.warning("Channel skipped because id is unknown: peer=%s title=%s", target.peer, info.title)
                    continue
                if info.protected_content:
                    LOGGER.warning(
                        "Protected channel configured for local download attempt: peer=%s title=%s",
                        full_channel_peer_id(info.id),
                        info.title,
                    )
                else:
                    LOGGER.warning("Configured channel: peer=%s title=%s", full_channel_peer_id(info.id), info.title)
                entities.append(entity)
                targets_by_id[channel_id] = target

            if not entities:
                raise RuntimeError("no usable channels configured")

            channel_media_dirs = {
                channel_id: (target.download_dir or settings.download_dir) / channel_storage_folder_name(channel_id)
                for channel_id, target in targets_by_id.items()
            }
            channel_text_dirs = {
                channel_id: (target.text_dir or settings.text_dir) / channel_storage_folder_name(channel_id)
                for channel_id, target in targets_by_id.items()
            }
            archive_lock = asyncio.Lock()

            async def archive_one(message: Message) -> None:
                async with archive_lock:
                    channel_id = normalized_channel_id(getattr(message, "chat_id", None))
                    target = targets_by_id.get(channel_id)
                    if target is None:
                        LOGGER.warning("Channel message skipped because peer is not configured: chat=%s message=%s", getattr(message, "chat_id", None), message.id)
                        return
                    if not message_in_date_range(message, start_date=target.start_date, end_date=target.end_date):
                        return
                    if has_protected_content(message) and not target.try_protected_content:
                        LOGGER.warning(
                            "Protected channel message skipped: chat=%s message=%s",
                            getattr(message, "chat_id", None),
                            message.id,
                        )
                        return
                    if has_protected_content(message):
                        LOGGER.warning(
                            "Protected channel message local download attempt: chat=%s message=%s",
                            getattr(message, "chat_id", None),
                            message.id,
                        )
                    source_key = f"channel_{channel_id}"
                    await archive_message(
                        client,
                        database,
                        settings,
                        message,
                        source_key=source_key,
                        log_label="Channel",
                        apply_saved_blocks=False,
                        archive_text=target.archive_text,
                        allowed_extensions=target.allowed_extensions,
                        allowed_media_types=target.media_types,
                        password_file=channel_settings.password_file,
                        strip_archive_passwords=channel_settings.strip_archive_passwords,
                        media_root_dir=channel_media_dirs.get(channel_id),
                        text_root_dir=channel_text_dirs.get(channel_id),
                    )
                    if target.download_delay_seconds > 0:
                        await asyncio.sleep(target.download_delay_seconds)

            @client.on(events.NewMessage(chats=entities))
            async def handle_channel_message(event: events.NewMessage.Event) -> None:
                await archive_one(event.message)

            periodic_task: asyncio.Task[None] | None = None
            if settings.recent_scan_interval_seconds > 0 and any(target.recent_scan_limit > 0 for target in targets_by_id.values()):
                periodic_task = asyncio.create_task(
                    periodic_channel_recent_scan_loop(client, settings, entities, targets_by_id, archive_one=archive_one)
                )
                LOGGER.warning(
                    "Periodic channel recent scan enabled: interval_seconds=%s limit=%s",
                    settings.recent_scan_interval_seconds,
                    settings.recent_scan_limit,
                )

            if settings.retry_partials_on_start:
                entities_by_id = {
                    normalized_channel_id(getattr(entity, "id", None)): entity
                    for entity in entities
                    if getattr(entity, "id", None) is not None
                }
                await retry_partial_channel_messages(client, settings, entities_by_id, archive_one=archive_one)
            for entity in entities:
                channel_id = normalized_channel_id(getattr(entity, "id", None))
                target = targets_by_id.get(channel_id)
                if target is not None and target.archive_existing:
                    await scan_existing_channel_messages(
                        client,
                        database,
                        settings,
                        entity,
                        archive_one=archive_one,
                        limit=target.existing_scan_limit,
                        start_date=target.start_date,
                        end_date=target.end_date,
                    )

            LOGGER.warning("Listening for new channel media")
            try:
                await client.run_until_disconnected()
            finally:
                if periodic_task is not None:
                    periodic_task.cancel()
                    try:
                        await periodic_task
                    except asyncio.CancelledError:
                        pass
    finally:
        database.close()


async def run_saved_stats(*, limit: int | None = None, progress_every: int = 1000) -> None:
    settings = load_saved_archiver_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database = Database(settings.db_path)
    client = TelegramClient(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        proxy=settings.proxy,
    )

    try:
        async with client:
            stats = await collect_saved_stats(client, database, settings, limit=limit, progress_every=progress_every)
            print(format_saved_stats(stats))
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise SystemExit(
                "Saved Messages session is locked. Stop the saved-archiver service before running saved-stats."
            ) from exc
        raise
    finally:
        database.close()

async def run_channels_list(*, include_groups: bool = False, limit: int | None = None) -> None:
    settings = load_telethon_session_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    client = TelegramClient(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        proxy=settings.proxy,
    )

    try:
        async with client:
            infos = await collect_joined_channel_infos(client, include_groups=include_groups, limit=limit)
            print(format_channel_list(infos))
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise SystemExit(
                "Telegram session is locked. Stop the saved-archiver service before running channels-list."
            ) from exc
        raise


async def run_channel_check(
    peer: str,
    *,
    limit: int = 20,
    download_sample: bool = False,
    max_sample_bytes: int = 50 * 1024 * 1024,
    try_protected_content: bool = False,
) -> None:
    settings = load_telethon_session_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    client = TelegramClient(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        proxy=settings.proxy,
    )

    try:
        async with client:
            result = await check_channel(
                client,
                peer,
                limit=limit,
                download_sample=download_sample,
                max_sample_bytes=max_sample_bytes,
                try_protected_content=try_protected_content,
            )
            print(format_channel_check_result(result))
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise SystemExit(
                "Telegram session is locked. Stop the saved-archiver service before running channel-check."
            ) from exc
        raise


async def collect_joined_channel_infos(
    client: TelegramClient,
    *,
    include_groups: bool = False,
    limit: int | None = None,
) -> list[ChannelInfo]:
    infos: list[ChannelInfo] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not is_channel_entity(entity):
            continue
        broadcast = bool(getattr(entity, "broadcast", False))
        if not include_groups and not broadcast:
            continue
        infos.append(channel_info_from_entity(entity))
        if limit is not None and len(infos) >= limit:
            break
    return infos


async def check_channel(
    client: TelegramClient,
    peer: str,
    *,
    limit: int = 20,
    download_sample: bool = False,
    max_sample_bytes: int = 50 * 1024 * 1024,
    try_protected_content: bool = False,
) -> ChannelCheckResult:
    if limit <= 0:
        raise ValueError("limit must be greater than 0")

    entity = await resolve_channel_entity(client, peer)
    info = channel_info_from_entity(entity)
    scanned_messages = 0
    media_messages = 0
    protected_messages = 0
    sample_message: Message | None = None
    sample_ref: MediaRef | None = None

    async for message in client.iter_messages(entity, limit=limit):
        scanned_messages += 1
        message_protected = has_protected_content(message)
        if message_protected:
            protected_messages += 1

        ref = media_ref_from_message(message)
        if ref is None:
            continue

        media_messages += 1
        if sample_message is None and (try_protected_content or not message_protected):
            sample_message = message
            sample_ref = ref

    sample_download_status = "not_requested"
    sample_download_detail = "pass --download-sample to try one temporary media download"
    if download_sample:
        if (info.protected_content or protected_messages) and not try_protected_content:
            sample_download_status = "skipped_protected"
            sample_download_detail = "protected content flag found on channel or sampled messages"
        elif sample_message is None or sample_ref is None:
            sample_download_status = "skipped_no_media"
            sample_download_detail = "no unprotected media message found in sampled messages"
        else:
            sample_download_status, sample_download_detail = await download_channel_sample(
                client,
                sample_message,
                sample_ref,
                max_sample_bytes=max_sample_bytes,
            )

    return ChannelCheckResult(
        peer=peer,
        id=info.id,
        title=info.title,
        username=info.username,
        protected_content=info.protected_content,
        scanned_messages=scanned_messages,
        media_messages=media_messages,
        protected_messages=protected_messages,
        sample_download_status=sample_download_status,
        sample_download_detail=sample_download_detail,
    )


async def download_channel_sample(
    client: TelegramClient,
    message: Message,
    ref: MediaRef,
    *,
    max_sample_bytes: int,
) -> tuple[str, str]:
    if ref.file_size is not None and ref.file_size > max_sample_bytes:
        return (
            "skipped_too_large",
            f"sample_media_bytes={ref.file_size} max_sample_bytes={max_sample_bytes}",
        )

    with tempfile.TemporaryDirectory(prefix="telegram-archiver-check-") as temp_dir:
        temp_path = resumable_temp_path(Path(temp_dir), message.id, ref)
        try:
            downloaded_path = await download_media_resumable(client, message, ref, temp_path)
        except Exception as exc:
            LOGGER.exception("Channel sample download failed: message=%s", message.id)
            return ("failed", f"{type(exc).__name__}: {exc}")
        return ("success", f"downloaded_bytes={downloaded_path.stat().st_size}")


def channel_info_from_entity(entity: object) -> ChannelInfo:
    return ChannelInfo(
        id=getattr(entity, "id", None),
        title=display_peer_title(entity),
        username=getattr(entity, "username", None),
        broadcast=bool(getattr(entity, "broadcast", False)),
        megagroup=bool(getattr(entity, "megagroup", False)),
        protected_content=has_protected_content(entity),
    )


def channel_storage_roots(root_dir: Path, entities: list[object]) -> dict[int, Path]:
    roots: dict[int, Path] = {}
    for entity in entities:
        channel_id = normalized_channel_id(getattr(entity, "id", None))
        if channel_id == 0:
            continue
        roots[channel_id] = root_dir / channel_storage_folder_name(channel_id)
    return roots


def channel_storage_folder_name(channel_id: int) -> str:
    return f"peer-100{channel_id}"


async def resolve_channel_entity(client: TelegramClient, peer: str) -> object:
    try:
        return await client.get_entity(peer)
    except Exception:
        peer_id = parse_channel_peer_id(peer)
        if peer_id is None:
            raise

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not is_channel_entity(entity):
            continue
        entity_id = getattr(entity, "id", None)
        if entity_id == peer_id:
            return entity
    raise ValueError(f"channel not found in joined dialogs: {peer}")


def parse_channel_peer_id(peer: str) -> int | None:
    value = peer.strip()
    if not re.fullmatch(r"-?\d+", value):
        return None
    raw_id = int(value)
    if raw_id <= -1000000000000:
        return int(str(raw_id)[4:])
    return abs(raw_id)


def normalized_channel_id(value: int | None) -> int:
    if value is None:
        return 0
    if value <= -1000000000000:
        return int(str(value)[4:])
    return abs(value)


def full_channel_peer_id(channel_id: int | None) -> str:
    if channel_id is None:
        return "-"
    return f"-100{channel_id}"


def is_channel_entity(entity: object) -> bool:
    return entity.__class__.__name__ == "Channel"


def has_protected_content(value: object) -> bool:
    return bool(getattr(value, "noforwards", False) or getattr(value, "no_forwards", False))


def display_peer_title(entity: object) -> str:
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first_name = getattr(entity, "first_name", None)
    last_name = getattr(entity, "last_name", None)
    name = " ".join(part for part in (first_name, last_name) if part)
    return name or str(getattr(entity, "id", "unknown"))


def format_channel_list(infos: list[ChannelInfo]) -> str:
    lines = [
        "Joined channels",
        f"count={len(infos)}",
    ]
    for info in infos:
        username = f"@{info.username}" if info.username else "-"
        flags = []
        if info.broadcast:
            flags.append("broadcast")
        if info.megagroup:
            flags.append("megagroup")
        if info.protected_content:
            flags.append("protected")
        flag_text = ",".join(flags) if flags else "-"
        lines.append(f"id={info.id} peer={full_channel_peer_id(info.id)} username={username} flags={flag_text} title={info.title}")
    return "\n".join(lines)


def format_channel_check_result(result: ChannelCheckResult) -> str:
    username = f"@{result.username}" if result.username else "-"
    lines = [
        "Channel check",
        f"peer={result.peer}",
        f"id={result.id}",
        f"title={result.title}",
        f"username={username}",
        f"protected_content={result.protected_content}",
        f"scanned_messages={result.scanned_messages}",
        f"media_messages={result.media_messages}",
        f"protected_messages={result.protected_messages}",
        f"sample_download_status={result.sample_download_status}",
        f"sample_download_detail={result.sample_download_detail}",
    ]
    return "\n".join(lines)


async def collect_saved_stats(
    client: TelegramClient,
    database: Database,
    settings: SavedArchiverSettings,
    *,
    limit: int | None = None,
    progress_every: int = 1000,
) -> SavedStats:
    stats = SavedStats()
    async for message in client.iter_messages("me", limit=limit):
        stats = SavedStats(
            scanned_messages=stats.scanned_messages + 1,
            media_messages=stats.media_messages,
            blocked_media=stats.blocked_media,
            already_archived_by_unique_id=stats.already_archived_by_unique_id,
            already_archived_by_file_id=stats.already_archived_by_file_id,
            download_candidates=stats.download_candidates,
        )

        if progress_every > 0 and stats.scanned_messages % progress_every == 0:
            LOGGER.warning("Saved Messages stats progress: scanned=%s media=%s candidates=%s", stats.scanned_messages, stats.media_messages, stats.download_candidates)

        ref = media_ref_from_message(message)
        if ref is None:
            continue

        if is_blocked_saved_message(settings, message, ref):
            stats = add_saved_stats(stats, media_messages=1, blocked_media=1)
            continue

        if database.get_saved_by_unique_id(ref.file_unique_id) is not None:
            stats = add_saved_stats(stats, media_messages=1, already_archived_by_unique_id=1)
            continue

        if database.get_saved_by_file_id(ref.file_id) is not None:
            stats = add_saved_stats(stats, media_messages=1, already_archived_by_file_id=1)
            continue

        stats = add_saved_stats(stats, media_messages=1, download_candidates=1)

    return stats

def add_saved_stats(
    stats: SavedStats,
    *,
    media_messages: int = 0,
    blocked_media: int = 0,
    already_archived_by_unique_id: int = 0,
    already_archived_by_file_id: int = 0,
    download_candidates: int = 0,
) -> SavedStats:
    return SavedStats(
        scanned_messages=stats.scanned_messages,
        media_messages=stats.media_messages + media_messages,
        blocked_media=stats.blocked_media + blocked_media,
        already_archived_by_unique_id=stats.already_archived_by_unique_id + already_archived_by_unique_id,
        already_archived_by_file_id=stats.already_archived_by_file_id + already_archived_by_file_id,
        download_candidates=stats.download_candidates + download_candidates,
    )

def format_saved_stats(stats: SavedStats) -> str:
    already_archived = stats.already_archived_by_unique_id + stats.already_archived_by_file_id
    lines = [
        "Saved Messages stats",
        f"scanned_messages={stats.scanned_messages}",
        f"media_messages={stats.media_messages}",
        f"blocked_media={stats.blocked_media}",
        f"already_archived={already_archived}",
        f"already_archived_by_unique_id={stats.already_archived_by_unique_id}",
        f"already_archived_by_file_id={stats.already_archived_by_file_id}",
        f"download_candidates={stats.download_candidates}",
    ]
    return "\n".join(lines)

async def archive_message(
    client: TelegramClient,
    database: Database,
    settings: SavedArchiverSettings,
    message: Message,
    *,
    source_key: str = "saved",
    log_label: str = "Saved Messages",
    apply_saved_blocks: bool = True,
    archive_text: bool = True,
    allowed_extensions: frozenset[str] = frozenset(),
    allowed_media_types: frozenset[str] = frozenset(),
    password_file: Path | None = None,
    strip_archive_passwords: bool = False,
    media_root_dir: Path | None = None,
    text_root_dir: Path | None = None,
) -> None:
    ref = media_ref_from_message(message, source_key=source_key)
    source = archive_source_name(source_key)
    if apply_saved_blocks and is_blocked_saved_message(settings, message, ref):
        LOGGER.warning("Blocked %s item skipped: message=%s", log_label, message.id)
        return

    if ref is not None and allowed_media_types and ref.media_type.casefold() not in allowed_media_types:
        LOGGER.info("Skipped %s media outside allowed media types: message=%s type=%s", log_label, message.id, ref.media_type)
        return

    if ref is not None and allowed_extensions and not media_ref_matches_extensions(ref, allowed_extensions):
        LOGGER.info("Skipped %s media outside allowed extensions: message=%s name=%s", log_label, message.id, ref.file_name)
        return

    if archive_text:
        archive_message_text(
            database,
            settings,
            message,
            source_key=source_key,
            log_label=log_label,
            text_root_dir=text_root_dir,
        )
    if ref is None:
        return
    record_processing = should_record_archive_processing(ref)

    existing = database.get_saved_by_unique_id(ref.file_unique_id)
    if existing is not None:
        if record_processing:
            record_archive_processing_metadata(
                database,
                message,
                source_key=source_key,
                original_name=ref.file_name,
                output_name=existing.original_name,
                status="duplicate_unique_id",
                password_matched=False,
                file_sha256=existing.sha256,
                final_path=existing.final_path,
            )
        database.resolve_source_archive_failure(source=source_key, source_message_id=message.id, media_type=ref.media_type)
        LOGGER.info("%s message %s already archived at %s", log_label, message.id, existing.final_path)
        return

    existing_by_file_id = database.get_saved_by_file_id(ref.file_id)
    if existing_by_file_id is not None:
        if record_processing:
            record_archive_processing_metadata(
                database,
                message,
                source_key=source_key,
                original_name=ref.file_name,
                output_name=existing_by_file_id.original_name,
                status="duplicate_file_id",
                password_matched=False,
                file_sha256=existing_by_file_id.sha256,
                final_path=existing_by_file_id.final_path,
            )
        database.record_alias(
            telegram_file_unique_id=ref.file_unique_id,
            telegram_file_id=ref.file_id,
            media_type=ref.media_type,
            latest_name=ref.file_name,
            source_message_id=message.id,
            file_sha256=existing_by_file_id.sha256,
        )
        record_message_metadata(database, message, ref, existing_by_file_id.sha256, existing_by_file_id.final_path, source_key=source_key)
        database.resolve_source_archive_failure(source=source_key, source_message_id=message.id, media_type=ref.media_type)
        LOGGER.warning(
            "Duplicate %s media skipped before download: message=%s path=%s",
            log_label,
            message.id,
            existing_by_file_id.final_path,
        )
        return

    temp_dir = settings.download_dir / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = resumable_temp_path(temp_dir, message.id, ref, source_key=source_key)

    try:
        actual_temp_path = await download_media_resumable(client, message, ref, temp_path)
        if actual_temp_path is None:
            return

        processed = maybe_strip_archive_password(
            actual_temp_path,
            ref,
            password_file=password_file,
            enabled=strip_archive_passwords,
            temp_dir=temp_dir,
            source_key=source_key,
        )
        processed_temp_path = processed.path
        processed_ref = processed.ref
        cleanup_paths = processed.cleanup_paths

        sha256 = sha256sum(processed_temp_path)
        existing_by_hash = database.get_saved_by_sha256(sha256)
        if existing_by_hash is not None:
            processed_temp_path.unlink(missing_ok=True)
            if processed_temp_path != actual_temp_path:
                actual_temp_path.unlink(missing_ok=True)
            for cleanup_path in cleanup_paths:
                if cleanup_path not in {processed_temp_path, actual_temp_path}:
                    cleanup_path.unlink(missing_ok=True)
            database.record_alias(
                telegram_file_unique_id=processed_ref.file_unique_id,
                telegram_file_id=processed_ref.file_id,
                media_type=processed_ref.media_type,
                latest_name=processed_ref.file_name,
                source_message_id=message.id,
                file_sha256=existing_by_hash.sha256,
            )
            record_message_metadata(database, message, processed_ref, existing_by_hash.sha256, existing_by_hash.final_path, source_key=source_key)
            if record_processing:
                record_archive_processing_metadata(
                    database,
                    message,
                    source_key=source_key,
                    original_name=processed.original_name,
                    output_name=existing_by_hash.original_name,
                    status=f"duplicate_hash_{processed.status}",
                    password_matched=processed.password_matched,
                    file_sha256=existing_by_hash.sha256,
                    final_path=existing_by_hash.final_path,
                    part_group=processed.part_group,
                    part_count=processed.part_count,
                    error_message=processed.error_message,
                )
            database.resolve_source_archive_failure(source=source_key, source_message_id=message.id, media_type=processed_ref.media_type)
            LOGGER.warning("Duplicate %s media skipped: message=%s path=%s", log_label, message.id, existing_by_hash.final_path)
            return

        final_name = build_storage_name(processed_ref, sha256)
        target_root = media_root_dir or settings.download_dir
        target_dir = media_storage_dir(target_root, processed_ref.media_type)
        target_dir.mkdir(parents=True, exist_ok=True)
        final_path = unique_target_path(target_dir / final_name)
        processed_temp_path.replace(final_path)
        if processed_temp_path != actual_temp_path:
            actual_temp_path.unlink(missing_ok=True)

        try:
            database.record_saved_file(
                sha256=sha256,
                final_path=str(final_path),
                original_name=processed_ref.file_name,
                media_type=processed_ref.media_type,
                mime_type=processed_ref.mime_type,
                file_size=processed_ref.file_size or final_path.stat().st_size,
                source_chat_id=getattr(message, "chat_id", None),
                source_message_id=message.id,
                forwarded_from=describe_forward_source(message),
                telegram_file_unique_id=processed_ref.file_unique_id,
                telegram_file_id=processed_ref.file_id,
            )
            record_message_metadata(database, message, processed_ref, sha256, str(final_path), source_key=source_key)
            if record_processing:
                record_archive_processing_metadata(
                    database,
                    message,
                    source_key=source_key,
                    original_name=processed.original_name,
                    output_name=processed.output_name,
                    status=processed.status,
                    password_matched=processed.password_matched,
                    file_sha256=sha256,
                    final_path=str(final_path),
                    part_group=processed.part_group,
                    part_count=processed.part_count,
                    error_message=processed.error_message,
                )
            database.resolve_source_archive_failure(source=source_key, source_message_id=message.id, media_type=processed_ref.media_type)
        except Exception:
            final_path.replace(processed_temp_path)
            raise
        for cleanup_path in cleanup_paths:
            if cleanup_path not in {processed_temp_path, actual_temp_path}:
                cleanup_path.unlink(missing_ok=True)
        LOGGER.warning("Archived %s media: message=%s path=%s", log_label, message.id, final_path)
    except Exception as exc:
        # Keep the .part file so the next source run can resume it.
        classification = classify_archive_exception(exc)
        database.record_source_archive_failure(
            source=source_key,
            source_message_id=message.id,
            media_type=ref.media_type,
            original_name=ref.file_name,
            file_size=ref.file_size,
            error_kind=classification.kind,
            error_class=exc.__class__.__name__,
            error_message=classification.message,
            retryable=classification.retryable,
            temp_path=str(temp_path),
        )
        if record_processing:
            record_archive_processing_metadata(
                database,
                message,
                source_key=source_key,
                original_name=ref.file_name,
                output_name=None,
                status=archive_processing_failure_status(exc),
                password_matched=False,
                file_sha256=None,
                final_path=None,
                part_group=archive_processing_part_group(ref),
                error_message=classification.message,
            )
        LOGGER.exception("Failed to archive %s media: message=%s", log_label, message.id)

def archive_source_name(source_key: str) -> str:
    return "channels" if source_key.startswith("channel_") else "saved"

def should_record_archive_processing(ref: MediaRef) -> bool:
    return media_ref_matches_extensions(ref, DEFAULT_ARCHIVE_EXTENSIONS)

def record_archive_processing_metadata(
    database: Database,
    message: Message,
    *,
    source_key: str,
    original_name: str | None,
    output_name: str | None,
    status: str,
    password_matched: bool,
    file_sha256: str | None,
    final_path: str | None,
    part_group: str | None = None,
    part_count: int | None = None,
    error_message: str | None = None,
) -> None:
    database.record_archive_processing(
        source=archive_source_name(source_key),
        source_key=source_key,
        source_chat_id=getattr(message, "chat_id", None),
        source_message_id=message.id,
        original_name=original_name,
        output_name=output_name,
        status=status,
        password_matched=password_matched,
        file_sha256=file_sha256,
        final_path=final_path,
        part_group=part_group,
        part_count=part_count,
        error_message=error_message,
    )

def archive_processing_failure_status(exc: Exception) -> str:
    message = str(exc).casefold()
    if "multipart archive deferred" in message:
        return "deferred_multipart"
    return "failed"

def archive_processing_part_group(ref: MediaRef) -> str | None:
    part = multipart_archive_part(ref.file_name)
    return part.group_key if part is not None else None

def classify_archive_exception(exc: Exception) -> ArchiveErrorClassification:
    error_class = exc.__class__.__name__
    raw_message = str(exc).strip()
    message = raw_message or error_class
    if error_class == "FileReferenceExpiredError":
        return ArchiveErrorClassification(
            kind="expired_file_reference",
            retryable=True,
            message=(
                "Telegram file reference expired. A later retry can work after refetching the message; "
                "self-destructing or inaccessible media may keep failing."
            ),
        )
    if "FloodWait" in error_class:
        return ArchiveErrorClassification(kind="rate_limited", retryable=True, message=message)
    if error_class in {"TimeoutError", "ConnectionError"} or "Network" in error_class:
        return ArchiveErrorClassification(kind="network", retryable=True, message=message)
    if error_class == "RuntimeError" and "incomplete download" in raw_message:
        return ArchiveErrorClassification(kind="incomplete_download", retryable=True, message=message)
    if isinstance(exc, OSError):
        return ArchiveErrorClassification(kind="filesystem", retryable=True, message=message)
    return ArchiveErrorClassification(kind="unknown", retryable=True, message=message)

def is_blocked_saved_message(
    settings: SavedArchiverSettings,
    message: Message,
    ref: MediaRef | None = None,
) -> bool:
    forward = getattr(message, "forward", None)
    if forward is not None and getattr(forward, "chat_id", None) in settings.blocked_forward_chat_ids:
        return True
    if not settings.blocked_forward_keywords:
        return False
    haystack = blocked_keyword_haystack(message, ref)
    return any(keyword in haystack for keyword in settings.blocked_forward_keywords)

def is_blocked_forward_source(settings: SavedArchiverSettings, message: Message) -> bool:
    return is_blocked_saved_message(settings, message)

def blocked_keyword_haystack(message: Message, ref: MediaRef | None = None) -> str:
    forward = getattr(message, "forward", None)
    forward_chat = getattr(forward, "chat", None) if forward else None
    forward_sender = getattr(forward, "sender", None) if forward else None
    parts = [
        getattr(forward_chat, "title", None),
        getattr(forward_chat, "username", None),
        getattr(forward_sender, "username", None),
        getattr(forward_sender, "first_name", None),
        getattr(forward_sender, "last_name", None),
    ]
    return " ".join(str(part).casefold() for part in parts if part is not None)

def media_ref_matches_extensions(ref: MediaRef, allowed_extensions: frozenset[str]) -> bool:
    if not allowed_extensions:
        return True
    file_name = (ref.file_name or "").casefold()
    if file_name and any(file_name.endswith(extension) for extension in allowed_extensions):
        return True
    if file_name and multipart_archive_part(ref.file_name) is not None and allowed_extensions & DEFAULT_ARCHIVE_EXTENSIONS:
        return True
    return ref.extension.casefold() in allowed_extensions


def maybe_strip_archive_password(
    archive_path: Path,
    ref: MediaRef,
    *,
    password_file: Path | None,
    enabled: bool,
    temp_dir: Path | None = None,
    source_key: str = "saved",
) -> ProcessedArchive:
    if not enabled:
        return archive_processing_result(archive_path, ref, status="not_requested")
    if not media_ref_matches_extensions(ref, DEFAULT_ARCHIVE_EXTENSIONS):
        return archive_processing_result(archive_path, ref, status="not_archive")
    if password_file is None:
        LOGGER.warning("Archive password stripping is enabled but CHANNEL_ARCHIVE_PASSWORD_FILE is not set")
        return archive_processing_result(archive_path, ref, status="not_configured")
    if not password_file.exists():
        LOGGER.warning("Archive password file not found: path=%s", password_file)
        return archive_processing_result(archive_path, ref, status="not_configured")

    seven_zip = shutil.which("7z") or shutil.which("7zz")
    if seven_zip is None:
        LOGGER.warning("Archive password stripping requires 7z or 7zz in PATH")
        return archive_processing_result(archive_path, ref, status="tool_missing")

    multipart = collect_multipart_temp_parts(temp_dir, source_key, archive_path, ref) if temp_dir is not None else None
    if multipart is not None:
        return maybe_strip_multipart_archive_password(
            seven_zip,
            archive_path,
            ref,
            password_file=password_file,
            multipart=multipart,
        )

    if archive_test_succeeds(seven_zip, archive_path, password=None):
        LOGGER.info("Archive is not password-protected or can be read without a password: path=%s", archive_path)
        return archive_processing_result(archive_path, ref, status="plain")

    passwords = load_archive_passwords(password_file)
    if not passwords:
        LOGGER.warning("Archive password file is empty: path=%s", password_file)
        return archive_processing_result(archive_path, ref, status="password_file_empty")

    for password in passwords:
        if not archive_test_succeeds(seven_zip, archive_path, password=password):
            continue
        unlocked_path = strip_archive_password(seven_zip, archive_path, ref, password)
        unlocked_ref = MediaRef(
            media_type=ref.media_type,
            file_id=ref.file_id,
            file_unique_id=ref.file_unique_id,
            file_name=unlocked_archive_name(ref),
            file_size=unlocked_path.stat().st_size,
            mime_type="application/zip",
            extension=".zip",
        )
        LOGGER.warning("Removed archive password: source=%s output=%s", archive_path, unlocked_path)
        return archive_processing_result(
            unlocked_path,
            unlocked_ref,
            cleanup_paths=(archive_path,),
            status="unlocked",
            password_matched=True,
            original_name=ref.file_name,
        )

    LOGGER.warning("Encrypted archive kept because no password matched: path=%s", archive_path)
    return archive_processing_result(archive_path, ref, status="encrypted_kept", error_message="no password matched")


def maybe_strip_multipart_archive_password(
    seven_zip: str,
    archive_path: Path,
    ref: MediaRef,
    *,
    password_file: Path,
    multipart: dict[int, tuple[Path, str, MultipartArchivePart]],
) -> ProcessedArchive:
    current_part = multipart_archive_part(ref.file_name or archive_path.name)
    if current_part is None:
        return archive_processing_result(archive_path, ref, status="plain")
    first = multipart.get(1)
    if first is None or not current_part.is_first:
        raise RuntimeError(f"multipart archive deferred until first volume is downloaded: name={ref.file_name}")

    passwords = load_archive_passwords(password_file)
    if not passwords:
        LOGGER.warning("Archive password file is empty: path=%s", password_file)
        return ProcessedArchive(archive_path, ref)

    with tempfile.TemporaryDirectory(prefix="telegram-archive-multipart-") as temp_dir_raw:
        staged_dir = Path(temp_dir_raw)
        first_path = stage_multipart_archive_parts(staged_dir, multipart)
        output_path = archive_path.with_name(f"{archive_path.stem}_unlocked.zip")
        if archive_test_succeeds(seven_zip, first_path, password=None):
            unlocked_path = strip_archive_password(seven_zip, first_path, ref, None, output_path=output_path)
            unlocked_ref = unlocked_media_ref(ref, unlocked_path)
            return archive_processing_result(
                unlocked_path,
                unlocked_ref,
                cleanup_paths=tuple(path for path, _, _ in multipart.values()),
                status="plain_multipart_merged",
                original_name=ref.file_name,
                part_group=current_part.group_key,
                part_count=len(multipart),
            )
        for password in passwords:
            if not archive_test_succeeds(seven_zip, first_path, password=password):
                continue
            unlocked_path = strip_archive_password(seven_zip, first_path, ref, password, output_path=output_path)
            unlocked_ref = unlocked_media_ref(ref, unlocked_path)
            LOGGER.warning("Removed multipart archive password: source=%s output=%s", archive_path, unlocked_path)
            return archive_processing_result(
                unlocked_path,
                unlocked_ref,
                cleanup_paths=tuple(path for path, _, _ in multipart.values()),
                status="unlocked_multipart",
                password_matched=True,
                original_name=ref.file_name,
                part_group=current_part.group_key,
                part_count=len(multipart),
            )

    raise RuntimeError(f"multipart archive deferred until all volumes and a matching password are available: name={ref.file_name}")


def unlocked_media_ref(ref: MediaRef, unlocked_path: Path) -> MediaRef:
    return MediaRef(
        media_type=ref.media_type,
        file_id=ref.file_id,
        file_unique_id=ref.file_unique_id,
        file_name=unlocked_archive_name(ref),
        file_size=unlocked_path.stat().st_size,
        mime_type="application/zip",
        extension=".zip",
    )


def archive_processing_result(
    path: Path,
    ref: MediaRef,
    *,
    cleanup_paths: tuple[Path, ...] = (),
    status: str,
    password_matched: bool = False,
    original_name: str | None = None,
    output_name: str | None = None,
    part_group: str | None = None,
    part_count: int | None = None,
    error_message: str | None = None,
) -> ProcessedArchive:
    part = multipart_archive_part(original_name or ref.file_name)
    return ProcessedArchive(
        path=path,
        ref=ref,
        cleanup_paths=cleanup_paths,
        status=status,
        password_matched=password_matched,
        original_name=original_name or ref.file_name,
        output_name=output_name or ref.file_name,
        part_group=part_group or (part.group_key if part is not None else None),
        part_count=part_count,
        error_message=error_message,
    )


def load_archive_passwords(password_file: Path) -> list[str]:
    passwords: list[str] = []
    for line in password_file.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        passwords.append(item)
    return passwords


def collect_multipart_temp_parts(
    temp_dir: Path | None,
    source_key: str,
    current_path: Path,
    ref: MediaRef,
) -> dict[int, tuple[Path, str, MultipartArchivePart]] | None:
    current_name = sanitize_filename(ref.file_name or ref.media_type)
    current_part = multipart_archive_part(current_name)
    if current_part is None or temp_dir is None:
        return None

    parts: dict[int, tuple[Path, str, MultipartArchivePart]] = {
        current_part.order: (current_path, current_name, current_part)
    }
    prefix = f"{sanitize_filename(source_key)}_"
    for path in temp_dir.glob(f"{prefix}*.part"):
        display_name = temp_display_name(path, source_key)
        if display_name is None:
            continue
        part = multipart_archive_part(display_name)
        if part is None or part.group_key != current_part.group_key:
            continue
        parts[part.order] = (path, display_name, part)

    if current_part.requires_sibling and len(parts) == 1:
        return None
    return parts


def temp_display_name(path: Path, source_key: str) -> str | None:
    prefix = f"{sanitize_filename(source_key)}_"
    if not path.name.startswith(prefix) or not path.name.endswith(".part"):
        return None
    rest = path.name[len(prefix):-5]
    match = re.fullmatch(r"\d+_[0-9a-f]{16}_(.+)", rest)
    if not match:
        return None
    return match.group(1)


def multipart_archive_part(file_name: str | None) -> MultipartArchivePart | None:
    if not file_name:
        return None
    name = sanitize_filename(file_name)
    lower = name.casefold()

    match = re.fullmatch(r"(.+)\.part(\d+)\.rar", lower)
    if match:
        order = int(match.group(2))
        base = match.group(1)
        return MultipartArchivePart(
            group_key=f"{base}.part.rar",
            first_name=f"{base}.part1.rar",
            order=order,
            is_first=order == 1,
        )

    match = re.fullmatch(r"(.+)\.r(\d{2,3})", lower)
    if match:
        order = int(match.group(2)) + 2
        base = match.group(1)
        return MultipartArchivePart(
            group_key=f"{base}.rar",
            first_name=f"{base}.rar",
            order=order,
            is_first=False,
        )

    if lower.endswith(".rar"):
        return MultipartArchivePart(
            group_key=lower,
            first_name=lower,
            order=1,
            is_first=True,
            requires_sibling=True,
        )

    match = re.fullmatch(r"(.+)\.z(\d{2,3})", lower)
    if match:
        order = int(match.group(2))
        base = match.group(1)
        return MultipartArchivePart(
            group_key=f"{base}.zip",
            first_name=f"{base}.zip",
            order=order,
            is_first=False,
        )

    if lower.endswith(".zip"):
        return MultipartArchivePart(
            group_key=lower,
            first_name=lower,
            order=1,
            is_first=True,
            requires_sibling=True,
        )

    match = re.fullmatch(r"(.+)\.(\d{3})", lower)
    if match:
        order = int(match.group(2))
        base = match.group(1)
        return MultipartArchivePart(
            group_key=base,
            first_name=f"{base}.001",
            order=order,
            is_first=order == 1,
        )

    return None


def stage_multipart_archive_parts(
    staged_dir: Path,
    multipart: dict[int, tuple[Path, str, MultipartArchivePart]],
) -> Path:
    first_path: Path | None = None
    for order, (path, display_name, part) in sorted(multipart.items()):
        staged_path = staged_dir / sanitize_filename(display_name)
        staged_path.symlink_to(path)
        if order == 1:
            first_path = staged_path
    if first_path is None:
        first_name = next(iter(multipart.values()))[2].first_name
        first_path = staged_dir / first_name
    return first_path


def archive_test_succeeds(seven_zip: str, archive_path: Path, *, password: str | None) -> bool:
    args = [seven_zip, "t", "-y"]
    if password is not None:
        args.append(f"-p{password}")
    args.append(str(archive_path))
    result = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def strip_archive_password(
    seven_zip: str,
    archive_path: Path,
    ref: MediaRef,
    password: str | None,
    *,
    output_path: Path | None = None,
) -> Path:
    with tempfile.TemporaryDirectory(prefix="telegram-archive-unlock-") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        extract_dir = temp_dir / "contents"
        extract_dir.mkdir()
        extract_args = [seven_zip, "x", "-y", f"-o{extract_dir}"]
        if password is not None:
            extract_args.append(f"-p{password}")
        extract_args.append(str(archive_path))
        extract_result = subprocess.run(
            extract_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        if extract_result.returncode != 0:
            raise RuntimeError(f"failed to extract encrypted archive: {extract_result.stderr.strip()}")

        unlocked_path = output_path or archive_path.with_name(f"{archive_path.stem}_unlocked.zip")
        unlocked_path.unlink(missing_ok=True)
        pack_result = subprocess.run(
            [seven_zip, "a", "-tzip", str(unlocked_path), "."],
            cwd=extract_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        if pack_result.returncode != 0:
            raise RuntimeError(f"failed to repack unlocked archive: {pack_result.stderr.strip()}")
        return unlocked_path


def unlocked_archive_name(ref: MediaRef) -> str:
    original = sanitize_filename(ref.file_name or ref.media_type)
    stem = Path(original).stem or "archive"
    return f"{stem}_unlocked.zip"


def resumable_temp_path(temp_dir: Path, message_id: int, ref: MediaRef, *, source_key: str = "saved") -> Path:
    digest = hashlib.sha256(ref.file_id.encode("utf-8")).hexdigest()[:16]
    temp_base = sanitize_filename(ref.file_name or ref.media_type)
    if Path(temp_base).suffix.lower() == ref.extension.lower():
        display_name = temp_base
    else:
        display_name = f"{temp_base}{ref.extension}"
    return temp_dir / f"{sanitize_filename(source_key)}_{message_id}_{digest}_{display_name}.part"

async def download_media_resumable(
    client: TelegramClient,
    message: Message,
    ref: MediaRef,
    temp_path: Path,
) -> Path | None:
    expected_size = ref.file_size
    existing_size = temp_path.stat().st_size if temp_path.exists() else 0

    if expected_size is not None and existing_size > expected_size:
        LOGGER.warning(
            "Partial file is larger than expected; restarting: message=%s path=%s",
            message.id,
            temp_path,
        )
        temp_path.unlink(missing_ok=True)
        existing_size = 0

    if expected_size is not None and existing_size == expected_size:
        LOGGER.warning("Using complete partial file without re-download: message=%s path=%s", message.id, temp_path)
        return temp_path

    final_size, downloaded_any = await append_download(client, message, temp_path, existing_size, expected_size)
    if expected_size is not None and final_size != expected_size:
        final_size, downloaded_any = await retry_incomplete_download(
            client,
            message,
            temp_path,
            expected_size,
            final_size,
            downloaded_any,
            force_restart=bool(existing_size and final_size == existing_size),
        )

    if expected_size is not None and final_size != expected_size:
        final_size, fallback_downloaded = await download_media_fallback(client, message, temp_path)
        downloaded_any = downloaded_any or fallback_downloaded

    if expected_size is not None and final_size != expected_size:
        raise RuntimeError(f"incomplete download: expected {expected_size} bytes, got {final_size}")
    if final_size == 0 and not downloaded_any:
        LOGGER.info("Message %s has no downloadable media", message.id)
        temp_path.unlink(missing_ok=True)
        return None

    return temp_path


async def append_download(
    client: TelegramClient,
    message: Message,
    temp_path: Path,
    offset: int,
    expected_size: int | None,
) -> tuple[int, bool]:
    mode = "ab" if offset else "wb"
    if offset:
        LOGGER.warning(
            "Resuming media download: message=%s offset=%s path=%s",
            message.id,
            offset,
            temp_path,
        )
    else:
        LOGGER.warning("Starting media download: message=%s path=%s", message.id, temp_path)

    downloaded_any = False
    with temp_path.open(mode) as handle:
        async for chunk in client.iter_download(
            message,
            offset=offset,
            file_size=expected_size,
        ):
            if not chunk:
                continue
            handle.write(chunk)
            downloaded_any = True

    final_size = temp_path.stat().st_size if temp_path.exists() else 0
    return final_size, downloaded_any


async def retry_incomplete_download(
    client: TelegramClient,
    message: Message,
    temp_path: Path,
    expected_size: int,
    final_size: int,
    downloaded_any: bool,
    *,
    force_restart: bool,
    attempts: int = 3,
) -> tuple[int, bool]:
    for attempt in range(1, attempts + 1):
        if force_restart or final_size >= expected_size:
            LOGGER.warning(
                "Partial file made no progress; restarting from zero: message=%s path=%s",
                message.id,
                temp_path,
            )
            temp_path.unlink(missing_ok=True)
            offset = 0
            force_restart = False
        else:
            offset = final_size

        LOGGER.warning(
            "Retrying incomplete media download: message=%s attempt=%s offset=%s expected=%s got=%s",
            message.id,
            attempt,
            offset,
            expected_size,
            final_size,
        )
        final_size, retry_downloaded = await append_download(client, message, temp_path, offset, expected_size)
        downloaded_any = downloaded_any or retry_downloaded
        if final_size == expected_size:
            break

    return final_size, downloaded_any


async def download_media_fallback(
    client: TelegramClient,
    message: Message,
    temp_path: Path,
) -> tuple[int, bool]:
    LOGGER.warning(
        "Falling back to full Telethon media download: message=%s path=%s",
        message.id,
        temp_path,
    )
    temp_path.unlink(missing_ok=True)
    downloaded_path = await client.download_media(message, file=str(temp_path))
    if downloaded_path is None:
        return 0, False

    actual_path = Path(downloaded_path)
    if actual_path != temp_path and actual_path.exists():
        actual_path.replace(temp_path)
    final_size = temp_path.stat().st_size if temp_path.exists() else 0
    return final_size, final_size > 0

def archive_message_text(
    database: Database,
    settings: SavedArchiverSettings,
    message: Message,
    *,
    source_key: str = "saved",
    log_label: str = "Saved Messages",
    text_root_dir: Path | None = None,
) -> Path | None:
    text = getattr(message, "raw_text", None) or ""
    if not text.strip():
        return None

    body = build_text_archive_body(message, text)
    target_root = text_root_dir or settings.text_dir
    target_root.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=target_root,
            prefix=f"{sanitize_filename(source_key)}_{message.id}_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(body)
            temp_path = Path(handle.name)

        sha256 = sha256sum(temp_path)
        existing = database.get_saved_by_sha256(sha256)
        if existing is not None:
            temp_path.unlink(missing_ok=True)
            LOGGER.info("Duplicate %s text skipped: message=%s path=%s", log_label, message.id, existing.final_path)
            return Path(existing.final_path)

        final_name = f"{sanitize_filename(source_key)}_{message.id}__{sha256[:12]}.txt"
        final_path = unique_target_path(target_root / final_name)
        temp_path.replace(final_path)

        try:
            database.record_saved_file(
                sha256=sha256,
                final_path=str(final_path),
                original_name=f"message_{message.id}.txt",
                media_type="text",
                mime_type="text/plain",
                file_size=final_path.stat().st_size,
                source_chat_id=getattr(message, "chat_id", None),
                source_message_id=message.id,
                forwarded_from=describe_forward_source(message),
                telegram_file_unique_id=f"{source_key}-text:{message.id}",
                telegram_file_id=f"{source_key}-text:{message.id}",
            )
        except Exception:
            final_path.replace(temp_path)
            raise
        LOGGER.warning("Archived %s text: message=%s path=%s", log_label, message.id, final_path)
        return final_path
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        LOGGER.exception("Failed to archive %s text: message=%s", log_label, message.id)
        return None

def build_text_archive_body(message: Message, text: str) -> str:
    forward = getattr(message, "forward", None)
    lines = [
        f"message_id: {message.id}",
        f"chat_id: {getattr(message, 'chat_id', None)}",
        f"message_date: {isoformat_or_none(getattr(message, 'date', None))}",
        f"edit_date: {isoformat_or_none(getattr(message, 'edit_date', None))}",
        f"forwarded_sender_id: {getattr(forward, 'sender_id', None) if forward else None}",
        f"forwarded_chat_id: {getattr(forward, 'chat_id', None) if forward else None}",
        f"forwarded_channel_post: {getattr(forward, 'channel_post', None) if forward else None}",
        f"forwarded_date: {isoformat_or_none(getattr(forward, 'date', None)) if forward else None}",
        f"forwarded_post_author: {getattr(forward, 'post_author', None) if forward else None}",
        f"grouped_id: {getattr(message, 'grouped_id', None)}",
        f"reply_to_msg_id: {getattr(getattr(message, 'reply_to', None), 'reply_to_msg_id', None)}",
        "",
        "text:",
        text.rstrip(),
        "",
    ]
    return "\n".join(lines)

def media_ref_from_message(message: Message, *, source_key: str = "saved") -> MediaRef | None:
    if not getattr(message, "media", None):
        return None

    file_info = getattr(message, "file", None)
    document = getattr(message, "document", None)
    photo = getattr(message, "photo", None)
    if file_info is None and document is None and photo is None:
        return None

    file_id = str(getattr(document, "id", None) or getattr(photo, "id", None) or message.id)
    mime_type = getattr(file_info, "mime_type", None)
    file_name = getattr(file_info, "name", None)
    file_size = getattr(file_info, "size", None)

    if photo:
        media_type = "photo"
        mime_type = mime_type or "image/jpeg"
        extension = ".jpg"
    elif getattr(message, "video", None):
        media_type = "video"
        extension = pick_extension(file_name, mime_type, ".mp4")
    elif getattr(message, "audio", None):
        media_type = "audio"
        extension = pick_extension(file_name, mime_type, ".mp3")
    elif getattr(message, "voice", None):
        media_type = "voice"
        extension = pick_extension(file_name, mime_type, ".ogg")
    elif getattr(message, "gif", None):
        media_type = "animation"
        extension = pick_extension(file_name, mime_type, ".mp4")
    elif getattr(message, "sticker", None):
        media_type = "sticker"
        extension = pick_extension(file_name, mime_type, ".webp")
    elif document is not None or file_info is not None:
        media_type = "document"
        extension = pick_extension(file_name, mime_type, ".bin")
    else:
        return None

    return MediaRef(
        media_type=media_type,
        file_id=file_id,
        file_unique_id=f"{source_key}:{message.id}:{file_id}",
        file_name=file_name,
        file_size=file_size,
        mime_type=mime_type,
        extension=extension,
    )

def pick_extension(file_name: str | None, mime_type: str | None, default: str) -> str:
    if file_name:
        suffix = Path(file_name).suffix
        if suffix:
            return suffix.lower()
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type, strict=False)
        if guessed:
            return guessed
    return default

def record_message_metadata(
    database: Database,
    message: Message,
    ref: MediaRef,
    file_sha256: str | None,
    final_path: str | None,
    *,
    source_key: str = "saved",
) -> None:
    forward = getattr(message, "forward", None)
    media = getattr(message, "media", None)
    metadata = dict(
        source_message_id=message.id,
        file_sha256=file_sha256,
        final_path=final_path,
        message_date=isoformat_or_none(getattr(message, "date", None)),
        edit_date=isoformat_or_none(getattr(message, "edit_date", None)),
        text=getattr(message, "raw_text", None) or None,
        forwarded_sender_id=getattr(forward, "sender_id", None) if forward else None,
        forwarded_chat_id=getattr(forward, "chat_id", None) if forward else None,
        forwarded_channel_post=getattr(forward, "channel_post", None) if forward else None,
        forwarded_date=isoformat_or_none(getattr(forward, "date", None)) if forward else None,
        forwarded_post_author=getattr(forward, "post_author", None) if forward else None,
        grouped_id=getattr(message, "grouped_id", None),
        reply_to_msg_id=getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        media_type=ref.media_type,
        mime_type=ref.mime_type,
        original_name=ref.file_name,
        file_size=ref.file_size,
        width=getattr(media, "w", None),
        height=getattr(media, "h", None),
        duration=getattr(media, "duration", None),
        telegram_file_id=ref.file_id,
    )
    source = archive_source_name(source_key)
    database.record_source_message_metadata(
        source=source,
        source_key=source_key,
        source_chat_id=getattr(message, "chat_id", None),
        **metadata,
    )
    if source_key == "saved":
        database.record_saved_message_metadata(
            chat_id=getattr(message, "chat_id", None),
            **metadata,
        )

def isoformat_or_none(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)

def describe_forward_source(message: Message) -> str | None:
    forward = getattr(message, "forward", None)
    if forward is None:
        return None
    sender_id = getattr(forward, "sender_id", None)
    chat_id = getattr(forward, "chat_id", None)
    if sender_id is not None:
        return f"user:{sender_id}"
    if chat_id is not None:
        return f"chat:{chat_id}"
    return "forwarded"

if __name__ == "__main__":
    asyncio.run(run_archiver())
