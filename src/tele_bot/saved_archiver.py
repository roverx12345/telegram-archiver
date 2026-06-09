from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import tempfile
from dataclasses import dataclass
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
    blocked_forward_chat_ids: frozenset[int]
    proxy: tuple | None

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
        blocked_forward_chat_ids=parse_int_set(os.getenv("SAVED_BLOCKED_FORWARD_CHAT_IDS")),
        proxy=parse_telethon_proxy(os.getenv("TELETHON_PROXY")),
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

def parse_telethon_proxy(value: str | None) -> tuple | None:
    if not value or not value.strip():
        return None

    try:
        import socks
    except ImportError as exc:
        raise RuntimeError("TELETHON_PROXY requires the PySocks package") from exc

    parsed = urlparse(value.strip())
    proxy_types = {
        "socks5": socks.SOCKS5,
        "socks5h": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
        "https": socks.HTTP,
    }
    proxy_type = proxy_types.get(parsed.scheme.lower())
    if proxy_type is None:
        raise RuntimeError("TELETHON_PROXY must use socks5h://, socks5://, socks4://, http://, or https://")
    if not parsed.hostname or parsed.port is None:
        raise RuntimeError("TELETHON_PROXY must include host and port")

    rdns = parsed.scheme.lower() == "socks5h"
    return (proxy_type, parsed.hostname, parsed.port, rdns, parsed.username, parsed.password)

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

    async with client:
        me = await client.get_me()
        LOGGER.warning("Saved Messages archiver started for account id=%s", getattr(me, "id", "unknown"))

        if settings.archive_existing:
            LOGGER.warning("Scanning existing Saved Messages media")
            async for message in client.iter_messages("me", reverse=True):
                await archive_message(client, database, settings, message)
            LOGGER.warning("Existing Saved Messages scan finished")

        @client.on(events.NewMessage(chats="me"))
        async def handle_saved_message(event: events.NewMessage.Event) -> None:
            await archive_message(client, database, settings, event.message)

        LOGGER.warning("Listening for new Saved Messages media")
        await client.run_until_disconnected()

    database.close()

async def archive_message(
    client: TelegramClient,
    database: Database,
    settings: SavedArchiverSettings,
    message: Message,
) -> None:
    if is_blocked_forward_source(settings, message):
        LOGGER.warning("Blocked Saved Messages forward skipped: message=%s", message.id)
        return

    text_path = archive_message_text(database, settings, message)
    ref = media_ref_from_message(message)
    if ref is None:
        return

    existing = database.get_saved_by_unique_id(ref.file_unique_id)
    if existing is not None:
        LOGGER.info("Message %s already archived at %s", message.id, existing.final_path)
        return

    existing_by_file_id = database.get_saved_by_file_id(ref.file_id)
    if existing_by_file_id is not None:
        database.record_alias(
            telegram_file_unique_id=ref.file_unique_id,
            telegram_file_id=ref.file_id,
            media_type=ref.media_type,
            latest_name=ref.file_name,
            source_message_id=message.id,
            file_sha256=existing_by_file_id.sha256,
        )
        record_message_metadata(database, message, ref, existing_by_file_id.sha256, existing_by_file_id.final_path)
        LOGGER.warning(
            "Duplicate Saved Messages media skipped before download: message=%s path=%s",
            message.id,
            existing_by_file_id.final_path,
        )
        return

    temp_dir = settings.download_dir / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = resumable_temp_path(temp_dir, message.id, ref)

    try:
        actual_temp_path = await download_media_resumable(client, message, ref, temp_path)
        if actual_temp_path is None:
            return

        sha256 = sha256sum(actual_temp_path)
        existing_by_hash = database.get_saved_by_sha256(sha256)
        if existing_by_hash is not None:
            actual_temp_path.unlink(missing_ok=True)
            database.record_alias(
                telegram_file_unique_id=ref.file_unique_id,
                telegram_file_id=ref.file_id,
                media_type=ref.media_type,
                latest_name=ref.file_name,
                source_message_id=message.id,
                file_sha256=existing_by_hash.sha256,
            )
            record_message_metadata(database, message, ref, existing_by_hash.sha256, existing_by_hash.final_path)
            LOGGER.warning("Duplicate Saved Messages media skipped: message=%s path=%s", message.id, existing_by_hash.final_path)
            return

        final_name = build_storage_name(ref, sha256)
        target_dir = media_storage_dir(settings.download_dir, ref.media_type)
        target_dir.mkdir(parents=True, exist_ok=True)
        final_path = unique_target_path(target_dir / final_name)
        actual_temp_path.replace(final_path)

        database.record_saved_file(
            sha256=sha256,
            final_path=str(final_path),
            original_name=ref.file_name,
            media_type=ref.media_type,
            mime_type=ref.mime_type,
            file_size=ref.file_size or final_path.stat().st_size,
            source_chat_id=getattr(message, "chat_id", None),
            source_message_id=message.id,
            forwarded_from=describe_forward_source(message),
            telegram_file_unique_id=ref.file_unique_id,
            telegram_file_id=ref.file_id,
        )
        record_message_metadata(database, message, ref, sha256, str(final_path))
        LOGGER.warning("Archived Saved Messages media: message=%s path=%s", message.id, final_path)
    except Exception:
        # Keep the .part file so the next saved-source run can resume it.
        LOGGER.exception("Failed to archive Saved Messages media: message=%s", message.id)

def is_blocked_forward_source(settings: SavedArchiverSettings, message: Message) -> bool:
    forward = getattr(message, "forward", None)
    if forward is None:
        return False
    chat_id = getattr(forward, "chat_id", None)
    return chat_id in settings.blocked_forward_chat_ids

def resumable_temp_path(temp_dir: Path, message_id: int, ref: MediaRef) -> Path:
    digest = hashlib.sha256(ref.file_id.encode("utf-8")).hexdigest()[:16]
    temp_base = sanitize_filename(ref.file_name or ref.media_type)
    if Path(temp_base).suffix.lower() == ref.extension.lower():
        display_name = temp_base
    else:
        display_name = f"{temp_base}{ref.extension}"
    return temp_dir / f"saved_{message_id}_{digest}_{display_name}.part"

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
            "Saved Messages partial file is larger than expected; restarting: message=%s path=%s",
            message.id,
            temp_path,
        )
        temp_path.unlink(missing_ok=True)
        existing_size = 0

    if expected_size is not None and existing_size == expected_size:
        LOGGER.warning("Using complete partial file without re-download: message=%s path=%s", message.id, temp_path)
        return temp_path

    mode = "ab" if existing_size else "wb"
    if existing_size:
        LOGGER.warning(
            "Resuming Saved Messages media download: message=%s offset=%s path=%s",
            message.id,
            existing_size,
            temp_path,
        )
    else:
        LOGGER.warning("Starting Saved Messages media download: message=%s path=%s", message.id, temp_path)

    downloaded_any = False
    with temp_path.open(mode) as handle:
        async for chunk in client.iter_download(
            message,
            offset=existing_size,
            file_size=expected_size,
        ):
            if not chunk:
                continue
            handle.write(chunk)
            downloaded_any = True

    final_size = temp_path.stat().st_size if temp_path.exists() else 0
    if expected_size is not None and final_size != expected_size:
        raise RuntimeError(f"incomplete download: expected {expected_size} bytes, got {final_size}")
    if final_size == 0 and not downloaded_any:
        LOGGER.info("Message %s has no downloadable media", message.id)
        temp_path.unlink(missing_ok=True)
        return None

    return temp_path

def archive_message_text(database: Database, settings: SavedArchiverSettings, message: Message) -> Path | None:
    text = getattr(message, "raw_text", None) or ""
    if not text.strip():
        return None

    body = build_text_archive_body(message, text)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=settings.text_dir,
            prefix=f"message_{message.id}_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(body)
            temp_path = Path(handle.name)

        sha256 = sha256sum(temp_path)
        existing = database.get_saved_by_sha256(sha256)
        if existing is not None:
            temp_path.unlink(missing_ok=True)
            LOGGER.info("Duplicate Saved Messages text skipped: message=%s path=%s", message.id, existing.final_path)
            return Path(existing.final_path)

        final_name = f"message_{message.id}__{sha256[:12]}.txt"
        final_path = unique_target_path(settings.text_dir / final_name)
        temp_path.replace(final_path)

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
            telegram_file_unique_id=f"saved-text:{message.id}",
            telegram_file_id=f"saved-text:{message.id}",
        )
        LOGGER.warning("Archived Saved Messages text: message=%s path=%s", message.id, final_path)
        return final_path
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        LOGGER.exception("Failed to archive Saved Messages text: message=%s", message.id)
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

def media_ref_from_message(message: Message) -> MediaRef | None:
    if not getattr(message, "media", None):
        return None

    file_info = getattr(message, "file", None)
    file_id = str(getattr(getattr(message, "document", None), "id", None) or getattr(getattr(message, "photo", None), "id", None) or message.id)
    mime_type = getattr(file_info, "mime_type", None)
    file_name = getattr(file_info, "name", None)
    file_size = getattr(file_info, "size", None)

    if getattr(message, "photo", None):
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
    else:
        media_type = "document"
        extension = pick_extension(file_name, mime_type, ".bin")

    return MediaRef(
        media_type=media_type,
        file_id=file_id,
        file_unique_id=f"saved:{message.id}:{file_id}",
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
) -> None:
    forward = getattr(message, "forward", None)
    media = getattr(message, "media", None)
    database.record_saved_message_metadata(
        source_message_id=message.id,
        file_sha256=file_sha256,
        final_path=final_path,
        chat_id=getattr(message, "chat_id", None),
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
