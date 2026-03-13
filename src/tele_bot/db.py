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

    def start_download_attempt(self, job_id: int) -> DownloadJob:
        self.connection.execute(
            """
            UPDATE download_jobs
            SET status = 'downloading',
                retry_count = retry_count + 1,
                last_error = NULL,
                updated_at = ?
            WHERE id = ?
              AND status NOT IN ('completed', 'duplicate')
            """,
            (utc_now(), job_id),
        )
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM download_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise LookupError("download job not found")
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
