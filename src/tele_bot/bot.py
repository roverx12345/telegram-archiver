from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .db import Database
from .media import MediaRef, build_storage_name, extract_media_ref, is_forwarded_message, sha256sum


LOGGER = logging.getLogger(__name__)


def build_application(settings: Settings, database: Database) -> Application:
    builder = Application.builder().token(settings.bot_token)
    if settings.local_bot_api_url:
        base = settings.local_bot_api_url.rstrip("/")
        builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
    application = builder.build()
    application.bot_data["settings"] = settings
    application.bot_data["db"] = database

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pair", pair_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    return application


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    auth_required = bool(settings.pair_code) and not settings.allow_unpaired_private
    text = (
        "把需要归档的媒体转发给我，我会自动下载、去重并保存。\n"
        "目前支持 document / video / audio / voice / animation / photo / sticker / video_note。"
    )
    if auth_required:
        text += "\n\n首次使用请先发送 `/pair <配对码>`。"
    await update.effective_message.reply_text(text, parse_mode="Markdown")


async def pair_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    database = get_db(context)
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None or user is None:
        return

    if chat.type != "private":
        await message.reply_text("配对只能在私聊里完成。")
        return

    if not settings.pair_code:
        database.authorize_user(user.id, chat.id, user.username, user.first_name)
        await message.reply_text("当前未配置配对码，已直接授权这个私聊会话。")
        return

    if not context.args:
        await message.reply_text("用法：`/pair <配对码>`", parse_mode="Markdown")
        return

    if context.args[0] != settings.pair_code:
        await message.reply_text("配对码不正确。")
        return

    database.authorize_user(user.id, chat.id, user.username, user.first_name)
    await message.reply_text("配对完成，后续转发媒体会自动归档。")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    database = get_db(context)
    files_count, users_count = database.stats()
    await update.effective_message.reply_text(
        f"已保存文件: {files_count}\n已授权会话: {users_count}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    database = get_db(context)
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None or user is None:
        return

    if chat.type != "private":
        await message.reply_text("这个 bot 只处理私聊里的转发消息。")
        return

    if not is_authorized(settings, database, user.id, chat.id):
        await message.reply_text("当前会话尚未配对，请先发送 `/pair <配对码>`。", parse_mode="Markdown")
        return

    if not is_forwarded_message(message):
        return

    media = extract_media_ref(message)
    if media is None:
        await message.reply_text("这条转发消息里没有可下载的媒体。")
        return

    existing = database.get_saved_by_unique_id(media.file_unique_id)
    if existing is not None:
        await message.reply_text(
            f"已存在，跳过下载。\nsha256: `{existing.sha256[:16]}...`\n路径: `{existing.final_path}`",
            parse_mode="Markdown",
        )
        return

    await message.reply_text(f"开始下载 `{media.file_name or media.media_type}`", parse_mode="Markdown")

    temp_dir = settings.download_dir / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / build_storage_name(media, media.file_unique_id.replace("/", "_"))
    try:
        telegram_file = await context.bot.get_file(media.file_id)
        await telegram_file.download_to_drive(custom_path=str(temp_path))

        sha256 = sha256sum(temp_path)
        existing = database.get_saved_by_sha256(sha256)
        if existing is not None:
            temp_path.unlink(missing_ok=True)
            database.record_alias(
                telegram_file_unique_id=media.file_unique_id,
                telegram_file_id=media.file_id,
                media_type=media.media_type,
                latest_name=media.file_name,
                source_message_id=message.message_id,
                file_sha256=existing.sha256,
            )
            await message.reply_text(
                f"内容已存在，已跳过保存。\nsha256: `{existing.sha256[:16]}...`\n路径: `{existing.final_path}`",
                parse_mode="Markdown",
            )
            return

        final_name = build_storage_name(media, sha256)
        final_path = unique_target_path(settings.download_dir / final_name)
        temp_path.replace(final_path)

        database.record_saved_file(
            sha256=sha256,
            final_path=str(final_path),
            original_name=media.file_name,
            media_type=media.media_type,
            mime_type=media.mime_type,
            file_size=media.file_size or final_path.stat().st_size,
            source_chat_id=chat.id,
            source_message_id=message.message_id,
            forwarded_from=describe_forward_source(message),
            telegram_file_unique_id=media.file_unique_id,
            telegram_file_id=media.file_id,
        )

        await message.reply_text(
            f"下载完成。\nsha256: `{sha256}`\n保存到: `{final_path}`",
            parse_mode="Markdown",
        )
    except Exception as exc:  # pragma: no cover - network errors are integration-level failures
        LOGGER.exception("Failed to process media %s", media.file_unique_id)
        temp_path.unlink(missing_ok=True)
        await message.reply_text(f"下载失败: {exc.__class__.__name__}: {exc}")


def is_authorized(settings: Settings, database: Database, user_id: int, chat_id: int) -> bool:
    if settings.allow_unpaired_private and not settings.pair_code:
        return True
    return database.is_authorized(user_id, chat_id)


def describe_forward_source(message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        return origin.__class__.__name__
    if getattr(message, "forward_from_chat", None) is not None:
        return "chat"
    if getattr(message, "forward_from", None) is not None:
        return "user"
    return None


def unique_target_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]
