from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from telegram import Message


INVALID_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class MediaRef:
    media_type: str
    file_id: str
    file_unique_id: str
    file_name: str | None
    file_size: int | None
    mime_type: str | None
    extension: str


def is_forwarded_message(message: Message) -> bool:
    return any(
        getattr(message, attr, None) is not None
        for attr in ("forward_origin", "forward_date", "forward_from_chat", "forward_from")
    )


def extract_media_ref(message: Message) -> MediaRef | None:
    if message.document:
        document = message.document
        return MediaRef(
            media_type="document",
            file_id=document.file_id,
            file_unique_id=document.file_unique_id,
            file_name=document.file_name,
            file_size=document.file_size,
            mime_type=document.mime_type,
            extension=_pick_extension(document.file_name, document.mime_type, ".bin"),
        )

    if message.video:
        video = message.video
        return MediaRef(
            media_type="video",
            file_id=video.file_id,
            file_unique_id=video.file_unique_id,
            file_name=video.file_name,
            file_size=video.file_size,
            mime_type=video.mime_type,
            extension=_pick_extension(video.file_name, video.mime_type, ".mp4"),
        )

    if message.audio:
        audio = message.audio
        return MediaRef(
            media_type="audio",
            file_id=audio.file_id,
            file_unique_id=audio.file_unique_id,
            file_name=audio.file_name,
            file_size=audio.file_size,
            mime_type=audio.mime_type,
            extension=_pick_extension(audio.file_name, audio.mime_type, ".mp3"),
        )

    if message.voice:
        voice = message.voice
        return MediaRef(
            media_type="voice",
            file_id=voice.file_id,
            file_unique_id=voice.file_unique_id,
            file_name=None,
            file_size=voice.file_size,
            mime_type=voice.mime_type,
            extension=_pick_extension(None, voice.mime_type, ".ogg"),
        )

    if message.animation:
        animation = message.animation
        return MediaRef(
            media_type="animation",
            file_id=animation.file_id,
            file_unique_id=animation.file_unique_id,
            file_name=animation.file_name,
            file_size=animation.file_size,
            mime_type=animation.mime_type,
            extension=_pick_extension(animation.file_name, animation.mime_type, ".mp4"),
        )

    if message.video_note:
        video_note = message.video_note
        return MediaRef(
            media_type="video_note",
            file_id=video_note.file_id,
            file_unique_id=video_note.file_unique_id,
            file_name=None,
            file_size=video_note.file_size,
            mime_type="video/mp4",
            extension=".mp4",
        )

    if message.sticker:
        sticker = message.sticker
        return MediaRef(
            media_type="sticker",
            file_id=sticker.file_id,
            file_unique_id=sticker.file_unique_id,
            file_name=None,
            file_size=sticker.file_size,
            mime_type="image/webp",
            extension=".webp",
        )

    if message.photo:
        photo = message.photo[-1]
        return MediaRef(
            media_type="photo",
            file_id=photo.file_id,
            file_unique_id=photo.file_unique_id,
            file_name=None,
            file_size=photo.file_size,
            mime_type="image/jpeg",
            extension=".jpg",
        )

    return None


def build_storage_name(ref: MediaRef, sha256: str) -> str:
    base_name = ref.file_name or ref.media_type
    sanitized = sanitize_filename(base_name)
    stem = Path(sanitized).stem or ref.media_type
    return f"{stem}__{sha256[:12]}{ref.extension}"


def media_storage_dir(download_dir: Path, media_type: str) -> Path:
    folder_by_type = {
        "photo": "photos",
        "video": "videos",
        "video_note": "videos",
        "animation": "videos",
        "audio": "audio",
        "voice": "audio",
        "document": "documents",
        "sticker": "stickers",
    }
    return download_dir / folder_by_type.get(media_type, "other")


def sanitize_filename(name: str) -> str:
    replaced = INVALID_FILENAME_CHARS.sub("_", name.strip())
    compact = re.sub(r"_+", "_", replaced).strip("._")
    return compact or "file"


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


def sha256sum(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _pick_extension(file_name: str | None, mime_type: str | None, default: str) -> str:
    if file_name:
        suffix = Path(file_name).suffix
        if suffix:
            return suffix.lower()
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type, strict=False)
        if guessed:
            return guessed
    return default
