from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

@dataclass(frozen=True)
class SavedFileRecord:
    sha256: str
    final_path: str
    original_name: str | None
    media_type: str
    mime_type: str | None
    file_size: int | None

@dataclass(frozen=True)
class DownloadJob:
    id: int
    source_chat_id: int
    source_message_id: int
    requester_user_id: int | None
    telegram_file_id: str
    telegram_file_unique_id: str
    media_type: str
    original_name: str | None
    mime_type: str | None
    file_size: int | None
    forwarded_from: str | None
    status: str
    retry_count: int
    last_error: str | None
    final_path: str | None
    file_sha256: str | None

@dataclass(frozen=True)
class SourceArchiveFailure:
    id: int
    source: str
    source_message_id: int
    media_type: str | None
    original_name: str | None
    file_size: int | None
    error_kind: str
    error_class: str
    error_message: str
    retryable: bool
    temp_path: str | None
    first_seen_at: str
    last_seen_at: str
    attempt_count: int
    resolved_at: str | None

class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                paired_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS saved_files (
                sha256 TEXT PRIMARY KEY,
                final_path TEXT NOT NULL,
                original_name TEXT,
                media_type TEXT NOT NULL,
                mime_type TEXT,
                file_size INTEGER,
                source_chat_id INTEGER,
                source_message_id INTEGER,
                forwarded_from TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS telegram_file_aliases (
                telegram_file_unique_id TEXT PRIMARY KEY,
                telegram_file_id TEXT NOT NULL,
                file_sha256 TEXT,
                latest_name TEXT,
                media_type TEXT NOT NULL,
                latest_seen_at TEXT NOT NULL,
                source_message_id INTEGER,
                FOREIGN KEY(file_sha256) REFERENCES saved_files(sha256)
            );

            CREATE TABLE IF NOT EXISTS saved_message_metadata (
                source_message_id INTEGER PRIMARY KEY,
                file_sha256 TEXT,
                final_path TEXT,
                chat_id INTEGER,
                message_date TEXT,
                edit_date TEXT,
                text TEXT,
                forwarded_sender_id INTEGER,
                forwarded_chat_id INTEGER,
                forwarded_channel_post INTEGER,
                forwarded_date TEXT,
                forwarded_post_author TEXT,
                grouped_id INTEGER,
                reply_to_msg_id INTEGER,
                media_type TEXT,
                mime_type TEXT,
                original_name TEXT,
                file_size INTEGER,
                width INTEGER,
                height INTEGER,
                duration REAL,
                telegram_file_id TEXT,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(file_sha256) REFERENCES saved_files(sha256)
            );

            CREATE TABLE IF NOT EXISTS source_message_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_key TEXT NOT NULL,
                source_chat_id INTEGER,
                source_message_id INTEGER NOT NULL,
                file_sha256 TEXT,
                final_path TEXT,
                message_date TEXT,
                edit_date TEXT,
                text TEXT,
                forwarded_sender_id INTEGER,
                forwarded_chat_id INTEGER,
                forwarded_channel_post INTEGER,
                forwarded_date TEXT,
                forwarded_post_author TEXT,
                grouped_id INTEGER,
                reply_to_msg_id INTEGER,
                media_type TEXT,
                mime_type TEXT,
                original_name TEXT,
                file_size INTEGER,
                width INTEGER,
                height INTEGER,
                duration REAL,
                telegram_file_id TEXT,
                recorded_at TEXT NOT NULL,
                UNIQUE(source_key, source_message_id),
                FOREIGN KEY(file_sha256) REFERENCES saved_files(sha256)
            );

            CREATE TABLE IF NOT EXISTS download_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                requester_user_id INTEGER,
                telegram_file_id TEXT NOT NULL,
                telegram_file_unique_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                original_name TEXT,
                mime_type TEXT,
                file_size INTEGER,
                forwarded_from TEXT,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                final_path TEXT,
                file_sha256 TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_chat_id, source_message_id)
            );

            CREATE TABLE IF NOT EXISTS source_archive_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                media_type TEXT,
                original_name TEXT,
                file_size INTEGER,
                error_kind TEXT NOT NULL,
                error_class TEXT NOT NULL,
                error_message TEXT NOT NULL,
                retryable INTEGER NOT NULL,
                temp_path TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                resolved_at TEXT,
                UNIQUE(source, source_message_id, media_type)
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def authorize_user(
        self,
        user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO authorized_users(user_id, chat_id, username, first_name, paired_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = excluded.username,
                first_name = excluded.first_name,
                paired_at = excluded.paired_at
            """,
            (user_id, chat_id, username, first_name, utc_now()),
        )
        self.connection.commit()

    def is_authorized(self, user_id: int, chat_id: int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM authorized_users WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        ).fetchone()
        return row is not None

    def get_saved_by_unique_id(self, telegram_file_unique_id: str) -> Optional[SavedFileRecord]:
        row = self.connection.execute(
            """
            SELECT sf.sha256, sf.final_path, sf.original_name, sf.media_type, sf.mime_type, sf.file_size
            FROM telegram_file_aliases tfa
            JOIN saved_files sf ON sf.sha256 = tfa.file_sha256
            WHERE tfa.telegram_file_unique_id = ?
            """,
            (telegram_file_unique_id,),
        ).fetchone()
        if row is None:
            return None
        return SavedFileRecord(
            sha256=row["sha256"],
            final_path=row["final_path"],
            original_name=row["original_name"],
            media_type=row["media_type"],
            mime_type=row["mime_type"],
            file_size=row["file_size"],
        )

    def get_saved_by_file_id(self, telegram_file_id: str) -> Optional[SavedFileRecord]:
        row = self.connection.execute(
            """
            SELECT sf.sha256, sf.final_path, sf.original_name, sf.media_type, sf.mime_type, sf.file_size
            FROM telegram_file_aliases tfa
            JOIN saved_files sf ON sf.sha256 = tfa.file_sha256
            WHERE tfa.telegram_file_id = ?
              AND tfa.file_sha256 IS NOT NULL
            ORDER BY tfa.latest_seen_at DESC
            LIMIT 1
            """,
            (telegram_file_id,),
        ).fetchone()
        if row is None:
            return None
        return SavedFileRecord(
            sha256=row["sha256"],
            final_path=row["final_path"],
            original_name=row["original_name"],
            media_type=row["media_type"],
            mime_type=row["mime_type"],
            file_size=row["file_size"],
        )

    def get_saved_by_sha256(self, sha256: str) -> Optional[SavedFileRecord]:
        row = self.connection.execute(
            """
            SELECT sha256, final_path, original_name, media_type, mime_type, file_size
            FROM saved_files
            WHERE sha256 = ?
            """,
            (sha256,),
        ).fetchone()
        if row is None:
            return None
        return SavedFileRecord(
            sha256=row["sha256"],
            final_path=row["final_path"],
            original_name=row["original_name"],
            media_type=row["media_type"],
            mime_type=row["mime_type"],
            file_size=row["file_size"],
        )

    def record_alias(
        self,
        telegram_file_unique_id: str,
        telegram_file_id: str,
        media_type: str,
        latest_name: str | None,
        source_message_id: int | None,
        file_sha256: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO telegram_file_aliases(
                telegram_file_unique_id,
                telegram_file_id,
                file_sha256,
                latest_name,
                media_type,
                latest_seen_at,
                source_message_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_file_unique_id) DO UPDATE SET
                telegram_file_id = excluded.telegram_file_id,
                file_sha256 = COALESCE(excluded.file_sha256, telegram_file_aliases.file_sha256),
                latest_name = excluded.latest_name,
                media_type = excluded.media_type,
                latest_seen_at = excluded.latest_seen_at,
                source_message_id = excluded.source_message_id
            """,
            (
                telegram_file_unique_id,
                telegram_file_id,
                file_sha256,
                latest_name,
                media_type,
                utc_now(),
                source_message_id,
            ),
        )
        self.connection.commit()

    def record_saved_file(
        self,
        *,
        sha256: str,
        final_path: str,
        original_name: str | None,
        media_type: str,
        mime_type: str | None,
        file_size: int | None,
        source_chat_id: int | None,
        source_message_id: int | None,
        forwarded_from: str | None,
        telegram_file_unique_id: str,
        telegram_file_id: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO saved_files(
                sha256,
                final_path,
                original_name,
                media_type,
                mime_type,
                file_size,
                source_chat_id,
                source_message_id,
                forwarded_from,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sha256,
                final_path,
                original_name,
                media_type,
                mime_type,
                file_size,
                source_chat_id,
                source_message_id,
                forwarded_from,
                utc_now(),
            ),
        )
        self.record_alias(
            telegram_file_unique_id=telegram_file_unique_id,
            telegram_file_id=telegram_file_id,
            media_type=media_type,
            latest_name=original_name,
            source_message_id=source_message_id,
            file_sha256=sha256,
        )
        self.connection.commit()

    def record_saved_message_metadata(
        self,
        *,
        source_message_id: int,
        file_sha256: str | None,
        final_path: str | None,
        chat_id: int | None,
        message_date: str | None,
        edit_date: str | None,
        text: str | None,
        forwarded_sender_id: int | None,
        forwarded_chat_id: int | None,
        forwarded_channel_post: int | None,
        forwarded_date: str | None,
        forwarded_post_author: str | None,
        grouped_id: int | None,
        reply_to_msg_id: int | None,
        media_type: str | None,
        mime_type: str | None,
        original_name: str | None,
        file_size: int | None,
        width: int | None,
        height: int | None,
        duration: float | None,
        telegram_file_id: str | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO saved_message_metadata(
                source_message_id, file_sha256, final_path, chat_id, message_date, edit_date, text,
                forwarded_sender_id, forwarded_chat_id, forwarded_channel_post, forwarded_date,
                forwarded_post_author, grouped_id, reply_to_msg_id, media_type, mime_type,
                original_name, file_size, width, height, duration, telegram_file_id, recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_message_id) DO UPDATE SET
                file_sha256 = COALESCE(excluded.file_sha256, saved_message_metadata.file_sha256),
                final_path = COALESCE(excluded.final_path, saved_message_metadata.final_path),
                chat_id = excluded.chat_id,
                message_date = excluded.message_date,
                edit_date = excluded.edit_date,
                text = excluded.text,
                forwarded_sender_id = excluded.forwarded_sender_id,
                forwarded_chat_id = excluded.forwarded_chat_id,
                forwarded_channel_post = excluded.forwarded_channel_post,
                forwarded_date = excluded.forwarded_date,
                forwarded_post_author = excluded.forwarded_post_author,
                grouped_id = excluded.grouped_id,
                reply_to_msg_id = excluded.reply_to_msg_id,
                media_type = excluded.media_type,
                mime_type = excluded.mime_type,
                original_name = excluded.original_name,
                file_size = excluded.file_size,
                width = excluded.width,
                height = excluded.height,
                duration = excluded.duration,
                telegram_file_id = excluded.telegram_file_id,
                recorded_at = excluded.recorded_at
            """,
            (
                source_message_id, file_sha256, final_path, chat_id, message_date, edit_date, text,
                forwarded_sender_id, forwarded_chat_id, forwarded_channel_post, forwarded_date,
                forwarded_post_author, grouped_id, reply_to_msg_id, media_type, mime_type,
                original_name, file_size, width, height, duration, telegram_file_id, utc_now(),
            ),
        )
        self.connection.commit()

    def record_source_message_metadata(
        self,
        *,
        source: str,
        source_key: str,
        source_chat_id: int | None,
        source_message_id: int,
        file_sha256: str | None,
        final_path: str | None,
        message_date: str | None,
        edit_date: str | None,
        text: str | None,
        forwarded_sender_id: int | None,
        forwarded_chat_id: int | None,
        forwarded_channel_post: int | None,
        forwarded_date: str | None,
        forwarded_post_author: str | None,
        grouped_id: int | None,
        reply_to_msg_id: int | None,
        media_type: str | None,
        mime_type: str | None,
        original_name: str | None,
        file_size: int | None,
        width: int | None,
        height: int | None,
        duration: float | None,
        telegram_file_id: str | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO source_message_metadata(
                source, source_key, source_chat_id, source_message_id,
                file_sha256, final_path, message_date, edit_date, text,
                forwarded_sender_id, forwarded_chat_id, forwarded_channel_post, forwarded_date,
                forwarded_post_author, grouped_id, reply_to_msg_id, media_type, mime_type,
                original_name, file_size, width, height, duration, telegram_file_id, recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key, source_message_id) DO UPDATE SET
                source = excluded.source,
                source_chat_id = excluded.source_chat_id,
                file_sha256 = COALESCE(excluded.file_sha256, source_message_metadata.file_sha256),
                final_path = COALESCE(excluded.final_path, source_message_metadata.final_path),
                message_date = excluded.message_date,
                edit_date = excluded.edit_date,
                text = excluded.text,
                forwarded_sender_id = excluded.forwarded_sender_id,
                forwarded_chat_id = excluded.forwarded_chat_id,
                forwarded_channel_post = excluded.forwarded_channel_post,
                forwarded_date = excluded.forwarded_date,
                forwarded_post_author = excluded.forwarded_post_author,
                grouped_id = excluded.grouped_id,
                reply_to_msg_id = excluded.reply_to_msg_id,
                media_type = excluded.media_type,
                mime_type = excluded.mime_type,
                original_name = excluded.original_name,
                file_size = excluded.file_size,
                width = excluded.width,
                height = excluded.height,
                duration = excluded.duration,
                telegram_file_id = excluded.telegram_file_id,
                recorded_at = excluded.recorded_at
            """,
            (
                source, source_key, source_chat_id, source_message_id,
                file_sha256, final_path, message_date, edit_date, text,
                forwarded_sender_id, forwarded_chat_id, forwarded_channel_post, forwarded_date,
                forwarded_post_author, grouped_id, reply_to_msg_id, media_type, mime_type,
                original_name, file_size, width, height, duration, telegram_file_id, utc_now(),
            ),
        )
        self.connection.commit()

    def create_or_get_download_job(
        self,
        *,
        source_chat_id: int,
        source_message_id: int,
        requester_user_id: int | None,
        telegram_file_id: str,
        telegram_file_unique_id: str,
        media_type: str,
        original_name: str | None,
        mime_type: str | None,
        file_size: int | None,
        forwarded_from: str | None,
    ) -> DownloadJob:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO download_jobs(
                source_chat_id,
                source_message_id,
                requester_user_id,
                telegram_file_id,
                telegram_file_unique_id,
                media_type,
                original_name,
                mime_type,
                file_size,
                forwarded_from,
                status,
                retry_count,
                last_error,
                final_path,
                file_sha256,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, ?, ?)
            ON CONFLICT(source_chat_id, source_message_id) DO UPDATE SET
                requester_user_id = excluded.requester_user_id,
                telegram_file_id = excluded.telegram_file_id,
                telegram_file_unique_id = excluded.telegram_file_unique_id,
                media_type = excluded.media_type,
                original_name = excluded.original_name,
                mime_type = excluded.mime_type,
                file_size = excluded.file_size,
                forwarded_from = excluded.forwarded_from,
                status = CASE
                    WHEN download_jobs.status = 'failed' THEN 'pending'
                    ELSE download_jobs.status
                END,
                retry_count = CASE
                    WHEN download_jobs.status = 'failed' THEN 0
                    ELSE download_jobs.retry_count
                END,
                last_error = CASE
                    WHEN download_jobs.status = 'failed' THEN NULL
                    ELSE download_jobs.last_error
                END,
                updated_at = excluded.updated_at
            """,
            (
                source_chat_id,
                source_message_id,
                requester_user_id,
                telegram_file_id,
                telegram_file_unique_id,
                media_type,
                original_name,
                mime_type,
                file_size,
                forwarded_from,
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.get_download_job(source_chat_id, source_message_id)

    def get_download_job(self, source_chat_id: int, source_message_id: int) -> DownloadJob:
        row = self.connection.execute(
            """
            SELECT *
            FROM download_jobs
            WHERE source_chat_id = ? AND source_message_id = ?
            """,
            (source_chat_id, source_message_id),
        ).fetchone()
        if row is None:
            raise LookupError("download job not found")
        return self._download_job_from_row(row)

    def get_recoverable_jobs(self, max_download_retries: int) -> list[DownloadJob]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM download_jobs
            WHERE status IN ('pending', 'downloading')
               OR (status = 'failed' AND retry_count < ?)
            ORDER BY created_at ASC, id ASC
            """,
            (max_download_retries,),
        ).fetchall()
        return [self._download_job_from_row(row) for row in rows]

    def list_jobs(
        self,
        *,
        source_chat_id: int,
        limit: int = 10,
        statuses: tuple[str, ...] | None = None,
    ) -> list[DownloadJob]:
        params: list[object] = [source_chat_id]
        sql = """
            SELECT *
            FROM download_jobs
            WHERE source_chat_id = ?
        """
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(sql, params).fetchall()
        return [self._download_job_from_row(row) for row in rows]

    def get_retryable_failed_jobs(
        self,
        *,
        source_chat_id: int,
        max_download_retries: int,
        limit: int = 20,
    ) -> list[DownloadJob]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM download_jobs
            WHERE source_chat_id = ?
              AND status = 'failed'
              AND retry_count < ?
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (source_chat_id, max_download_retries, limit),
        ).fetchall()
        return [self._download_job_from_row(row) for row in rows]

    def chat_job_stats(self, source_chat_id: int) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM download_jobs
            WHERE source_chat_id = ?
            GROUP BY status
            """,
            (source_chat_id,),
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def start_download_attempt(self, job_id: int) -> DownloadJob | None:
        cursor = self.connection.execute(
            """
            UPDATE download_jobs
            SET status = 'downloading',
                retry_count = retry_count + 1,
                last_error = NULL,
                updated_at = ?
            WHERE id = ?
              AND status NOT IN ('completed', 'duplicate', 'downloading')
            """,
            (utc_now(), job_id),
        )
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM download_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise LookupError("download job not found")
        if cursor.rowcount == 0:
            return None
        return self._download_job_from_row(row)

    def mark_job_completed(self, job_id: int, *, final_path: str, file_sha256: str) -> None:
        self._mark_job_terminal(
            job_id,
            status="completed",
            last_error=None,
            final_path=final_path,
            file_sha256=file_sha256,
        )

    def mark_job_duplicate(self, job_id: int, *, final_path: str, file_sha256: str) -> None:
        self._mark_job_terminal(
            job_id,
            status="duplicate",
            last_error=None,
            final_path=final_path,
            file_sha256=file_sha256,
        )

    def mark_job_failed(self, job_id: int, *, error: str) -> None:
        self._mark_job_terminal(
            job_id,
            status="failed",
            last_error=error,
            final_path=None,
            file_sha256=None,
        )

    def record_source_archive_failure(
        self,
        *,
        source: str,
        source_message_id: int,
        media_type: str | None,
        original_name: str | None,
        file_size: int | None,
        error_kind: str,
        error_class: str,
        error_message: str,
        retryable: bool,
        temp_path: str | None,
    ) -> None:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO source_archive_failures(
                source,
                source_message_id,
                media_type,
                original_name,
                file_size,
                error_kind,
                error_class,
                error_message,
                retryable,
                temp_path,
                first_seen_at,
                last_seen_at,
                attempt_count,
                resolved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
            ON CONFLICT(source, source_message_id, media_type) DO UPDATE SET
                original_name = excluded.original_name,
                file_size = excluded.file_size,
                error_kind = excluded.error_kind,
                error_class = excluded.error_class,
                error_message = excluded.error_message,
                retryable = excluded.retryable,
                temp_path = excluded.temp_path,
                last_seen_at = excluded.last_seen_at,
                attempt_count = source_archive_failures.attempt_count + 1,
                resolved_at = NULL
            """,
            (
                source,
                source_message_id,
                media_type,
                original_name,
                file_size,
                error_kind,
                error_class,
                error_message,
                1 if retryable else 0,
                temp_path,
                now,
                now,
            ),
        )
        self.connection.commit()

    def resolve_source_archive_failure(
        self,
        *,
        source: str,
        source_message_id: int,
        media_type: str | None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE source_archive_failures
            SET resolved_at = ?
            WHERE source = ?
              AND source_message_id = ?
              AND COALESCE(media_type, '') = COALESCE(?, '')
              AND resolved_at IS NULL
            """,
            (utc_now(), source, source_message_id, media_type),
        )
        self.connection.commit()

    def unresolved_source_archive_failures(self, *, limit: int = 20) -> list[SourceArchiveFailure]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM source_archive_failures
            WHERE resolved_at IS NULL
            ORDER BY last_seen_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._source_archive_failure_from_row(row) for row in rows]

    def job_stats(self) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM download_jobs
            GROUP BY status
            """
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def stats(self) -> tuple[int, int]:
        files_count = self.connection.execute("SELECT COUNT(*) AS c FROM saved_files").fetchone()["c"]
        users_count = self.connection.execute("SELECT COUNT(*) AS c FROM authorized_users").fetchone()["c"]
        return files_count, users_count

    def _mark_job_terminal(
        self,
        job_id: int,
        *,
        status: str,
        last_error: str | None,
        final_path: str | None,
        file_sha256: str | None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE download_jobs
            SET status = ?,
                last_error = ?,
                final_path = COALESCE(?, final_path),
                file_sha256 = COALESCE(?, file_sha256),
                updated_at = ?
            WHERE id = ?
            """,
            (status, last_error, final_path, file_sha256, utc_now(), job_id),
        )
        self.connection.commit()

    def _download_job_from_row(self, row: sqlite3.Row) -> DownloadJob:
        return DownloadJob(
            id=row["id"],
            source_chat_id=row["source_chat_id"],
            source_message_id=row["source_message_id"],
            requester_user_id=row["requester_user_id"],
            telegram_file_id=row["telegram_file_id"],
            telegram_file_unique_id=row["telegram_file_unique_id"],
            media_type=row["media_type"],
            original_name=row["original_name"],
            mime_type=row["mime_type"],
            file_size=row["file_size"],
            forwarded_from=row["forwarded_from"],
            status=row["status"],
            retry_count=row["retry_count"],
            last_error=row["last_error"],
            final_path=row["final_path"],
            file_sha256=row["file_sha256"],
        )

    def _source_archive_failure_from_row(self, row: sqlite3.Row) -> SourceArchiveFailure:
        return SourceArchiveFailure(
            id=row["id"],
            source=row["source"],
            source_message_id=row["source_message_id"],
            media_type=row["media_type"],
            original_name=row["original_name"],
            file_size=row["file_size"],
            error_kind=row["error_kind"],
            error_class=row["error_class"],
            error_message=row["error_message"],
            retryable=bool(row["retryable"]),
            temp_path=row["temp_path"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            attempt_count=row["attempt_count"],
            resolved_at=row["resolved_at"],
        )
