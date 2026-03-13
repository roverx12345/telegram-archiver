from __future__ import annotations

import logging
from pathlib import Path

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .db import Database, DownloadJob
from .media import MediaRef, build_storage_name, extract_media_ref, is_forwarded_message, sha256sum


LOGGER = logging.getLogger(__name__)


def build_application(settings: Settings, database: Database) -> Application:
    builder = Application.builder().token(settings.bot_token)
    if settings.local_bot_api_url:
        base = settings.local_bot_api_url.rstrip("/")
        builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
    builder = builder.post_init(post_init)
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
    job_stats = database.job_stats()
    await update.effective_message.reply_text(
        "已保存文件: {files}\n已授权会话: {users}\n待处理任务: {pending}\n下载中: {downloading}\n失败任务: {failed}".format(
            files=files_count,
            users=users_count,
            pending=job_stats.get("pending", 0),
            downloading=job_stats.get("downloading", 0),
            failed=job_stats.get("failed", 0),
        )
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

    job = database.create_or_get_download_job(
        source_chat_id=chat.id,
        source_message_id=message.message_id,
        requester_user_id=user.id,
        telegram_file_id=media.file_id,
        telegram_file_unique_id=media.file_unique_id,
        media_type=media.media_type,
        original_name=media.file_name,
        mime_type=media.mime_type,
        file_size=media.file_size,
        forwarded_from=describe_forward_source(message),
    )
    if job.status == "downloading":
        await message.reply_text("这条消息对应的下载任务已经在处理中。")
        return

    await process_download_job(
        bot=context.bot,
        settings=settings,
        database=database,
        job=job,
        notifier=message.reply_text,
        recovery_mode=False,
    )


async def process_download_job(
    *,
    bot: Bot,
    settings: Settings,
    database: Database,
    job: DownloadJob,
    notifier,
    recovery_mode: bool,
) -> None:
    if job.status in {"completed", "duplicate"}:
        await notifier(f"这条消息已经处理过了，结果路径: {job.final_path}")
        return

    if job.status == "failed" and job.retry_count >= settings.max_download_retries:
        await notifier(
            f"这条消息之前已失败 {job.retry_count} 次，超过重试上限 {settings.max_download_retries}。"
        )
        return

    media = media_ref_from_job(job)
    existing = database.get_saved_by_unique_id(job.telegram_file_unique_id)
    if existing is not None:
        database.mark_job_duplicate(job.id, final_path=existing.final_path, file_sha256=existing.sha256)
        await notifier(
            f"已存在，跳过下载。\nsha256: {existing.sha256[:16]}...\n路径: {existing.final_path}"
        )
        return

    started_job = database.start_download_attempt(job.id)
    prefix = "恢复下载" if recovery_mode else "开始下载"
    await notifier(
        f"{prefix} {media.file_name or media.media_type}（第 {started_job.retry_count} 次尝试）"
    )

    temp_dir = settings.download_dir / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / build_storage_name(media, media.file_unique_id.replace("/", "_"))
    try:
        telegram_file = await bot.get_file(media.file_id)
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
                source_message_id=job.source_message_id,
                file_sha256=existing.sha256,
            )
            database.mark_job_duplicate(job.id, final_path=existing.final_path, file_sha256=existing.sha256)
            await notifier(
                f"内容已存在，已跳过保存。\nsha256: {existing.sha256[:16]}...\n路径: {existing.final_path}"
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
            source_chat_id=job.source_chat_id,
            source_message_id=job.source_message_id,
            forwarded_from=job.forwarded_from,
            telegram_file_unique_id=media.file_unique_id,
            telegram_file_id=media.file_id,
        )
        database.mark_job_completed(job.id, final_path=str(final_path), file_sha256=sha256)

        await notifier(f"下载完成。\nsha256: {sha256}\n保存到: {final_path}")
    except Exception as exc:  # pragma: no cover - network errors are integration-level failures
        LOGGER.exception("Failed to process media %s", media.file_unique_id)
        temp_path.unlink(missing_ok=True)
        database.mark_job_failed(job.id, error=f"{exc.__class__.__name__}: {exc}")
        await notifier(f"下载失败: {exc.__class__.__name__}: {exc}")


async def post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    database: Database = application.bot_data["db"]
    jobs = database.get_recoverable_jobs(settings.max_download_retries)
    if not jobs:
        return

    LOGGER.info("Recovering %s download job(s) from local state", len(jobs))
    for job in jobs:
        await process_download_job(
            bot=application.bot,
            settings=settings,
            database=database,
            job=job,
            notifier=chat_notifier(application.bot, job.source_chat_id),
            recovery_mode=True,
        )


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


def media_ref_from_job(job: DownloadJob) -> MediaRef:
    file_name = job.original_name
    extension = Path(file_name).suffix if file_name and Path(file_name).suffix else default_extension(job.media_type)
    return MediaRef(
        media_type=job.media_type,
        file_id=job.telegram_file_id,
        file_unique_id=job.telegram_file_unique_id,
        file_name=file_name,
        file_size=job.file_size,
        mime_type=job.mime_type,
        extension=extension,
    )


def default_extension(media_type: str) -> str:
    return {
        "video": ".mp4",
        "audio": ".mp3",
        "voice": ".ogg",
        "animation": ".mp4",
        "photo": ".jpg",
        "sticker": ".webp",
        "video_note": ".mp4",
        "document": ".bin",
    }.get(media_type, ".bin")


def chat_notifier(bot: Bot, chat_id: int):
    async def _notify(text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text)

    return _notify
