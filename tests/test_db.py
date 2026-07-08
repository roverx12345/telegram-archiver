from pathlib import Path

from tele_bot.db import Database


def test_download_job_lifecycle(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    job = db.create_or_get_download_job(
        source_chat_id=1,
        source_message_id=2,
        requester_user_id=3,
        telegram_file_id="file-id",
        telegram_file_unique_id="unique-id",
        media_type="video",
        original_name="clip.mp4",
        mime_type="video/mp4",
        file_size=123,
        forwarded_from="chat",
    )

    assert job.status == "pending"
    assert job.retry_count == 0

    job = db.start_download_attempt(job.id)
    assert job.status == "downloading"
    assert job.retry_count == 1

    db.mark_job_failed(job.id, error="TimeoutError: timed out")
    failed = db.get_download_job(1, 2)
    assert failed.status == "failed"
    assert failed.last_error == "TimeoutError: timed out"

    recoverable = db.get_recoverable_jobs(max_download_retries=3)
    assert [item.id for item in recoverable] == [job.id]

    db.mark_job_completed(job.id, final_path="/tmp/clip.mp4", file_sha256="abc")
    completed = db.get_download_job(1, 2)
    assert completed.status == "completed"
    assert completed.final_path == "/tmp/clip.mp4"
    assert completed.file_sha256 == "abc"


def test_existing_job_is_reused(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    first = db.create_or_get_download_job(
        source_chat_id=10,
        source_message_id=20,
        requester_user_id=30,
        telegram_file_id="f1",
        telegram_file_unique_id="u1",
        media_type="document",
        original_name="a.bin",
        mime_type="application/octet-stream",
        file_size=456,
        forwarded_from="user",
    )
    db.mark_job_duplicate(first.id, final_path="/tmp/a.bin", file_sha256="sha")

    second = db.create_or_get_download_job(
        source_chat_id=10,
        source_message_id=20,
        requester_user_id=30,
        telegram_file_id="f2",
        telegram_file_unique_id="u2",
        media_type="document",
        original_name="b.bin",
        mime_type="application/octet-stream",
        file_size=789,
        forwarded_from="user",
    )

    assert second.id == first.id
    assert second.status == "duplicate"
    assert second.final_path == "/tmp/a.bin"


def test_list_jobs_and_retryable_failed_jobs(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    first = db.create_or_get_download_job(
        source_chat_id=99,
        source_message_id=1,
        requester_user_id=1,
        telegram_file_id="f1",
        telegram_file_unique_id="u1",
        media_type="video",
        original_name="one.mp4",
        mime_type="video/mp4",
        file_size=1,
        forwarded_from="chat",
    )
    second = db.create_or_get_download_job(
        source_chat_id=99,
        source_message_id=2,
        requester_user_id=1,
        telegram_file_id="f2",
        telegram_file_unique_id="u2",
        media_type="video",
        original_name="two.mp4",
        mime_type="video/mp4",
        file_size=2,
        forwarded_from="chat",
    )
    third = db.create_or_get_download_job(
        source_chat_id=99,
        source_message_id=3,
        requester_user_id=1,
        telegram_file_id="f3",
        telegram_file_unique_id="u3",
        media_type="video",
        original_name="three.mp4",
        mime_type="video/mp4",
        file_size=3,
        forwarded_from="chat",
    )

    db.start_download_attempt(first.id)
    db.mark_job_failed(first.id, error="network")
    db.start_download_attempt(second.id)
    db.mark_job_failed(second.id, error="timeout")
    db.start_download_attempt(second.id)
    db.mark_job_failed(second.id, error="timeout")
    db.start_download_attempt(second.id)
    db.mark_job_failed(second.id, error="timeout")
    db.mark_job_completed(third.id, final_path="/tmp/three.mp4", file_sha256="abc")

    jobs = db.list_jobs(source_chat_id=99, limit=10)
    assert [job.source_message_id for job in jobs] == [3, 2, 1]

    failed_only = db.list_jobs(source_chat_id=99, limit=10, statuses=("failed",))
    assert [job.source_message_id for job in failed_only] == [2, 1]

    retryable = db.get_retryable_failed_jobs(source_chat_id=99, max_download_retries=3, limit=10)
    assert [job.source_message_id for job in retryable] == [1]

    stats = db.chat_job_stats(99)
    assert stats["failed"] == 2
    assert stats["completed"] == 1



def test_failed_job_reforward_resets_retry_state(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    job = db.create_or_get_download_job(
        source_chat_id=1,
        source_message_id=2,
        requester_user_id=3,
        telegram_file_id="file-id",
        telegram_file_unique_id="unique-id",
        media_type="video",
        original_name="clip.mp4",
        mime_type="video/mp4",
        file_size=123,
        forwarded_from="chat",
    )
    for _ in range(3):
        db.start_download_attempt(job.id)
        db.mark_job_failed(job.id, error="network")

    reset = db.create_or_get_download_job(
        source_chat_id=1,
        source_message_id=2,
        requester_user_id=4,
        telegram_file_id="new-file-id",
        telegram_file_unique_id="new-unique-id",
        media_type="video",
        original_name="clip-new.mp4",
        mime_type="video/mp4",
        file_size=456,
        forwarded_from="chat",
    )

    assert reset.id == job.id
    assert reset.status == "pending"
    assert reset.retry_count == 0
    assert reset.last_error is None
    assert reset.telegram_file_id == "new-file-id"


def test_downloading_job_is_not_started_twice(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    job = db.create_or_get_download_job(
        source_chat_id=1,
        source_message_id=2,
        requester_user_id=3,
        telegram_file_id="file-id",
        telegram_file_unique_id="unique-id",
        media_type="video",
        original_name="clip.mp4",
        mime_type="video/mp4",
        file_size=123,
        forwarded_from="chat",
    )

    first = db.start_download_attempt(job.id)
    second = db.start_download_attempt(job.id)

    assert first is not None
    assert first.status == "downloading"
    assert second is None
    assert db.get_download_job(1, 2).retry_count == 1


def test_source_archive_failure_lifecycle(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    db.record_source_archive_failure(
        source="saved",
        source_message_id=123,
        media_type="video",
        original_name="clip.mp4",
        file_size=456,
        error_kind="expired_file_reference",
        error_class="FileReferenceExpiredError",
        error_message="expired",
        retryable=True,
        temp_path="/tmp/clip.part",
    )
    db.record_source_archive_failure(
        source="saved",
        source_message_id=123,
        media_type="video",
        original_name="clip.mp4",
        file_size=456,
        error_kind="network",
        error_class="TimeoutError",
        error_message="timeout",
        retryable=True,
        temp_path="/tmp/clip.part",
    )

    failures = db.unresolved_source_archive_failures()

    assert len(failures) == 1
    assert failures[0].attempt_count == 2
    assert failures[0].error_kind == "network"
    assert failures[0].retryable is True

    db.resolve_source_archive_failure(source="saved", source_message_id=123, media_type="video")

    assert db.unresolved_source_archive_failures() == []


def test_source_message_metadata_is_scoped_by_source_key(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")

    db.record_source_message_metadata(
        source="channels",
        source_key="channel_100",
        source_chat_id=-100100,
        source_message_id=7,
        file_sha256=None,
        final_path="/tmp/a.zip",
        message_date=None,
        edit_date=None,
        text=None,
        forwarded_sender_id=None,
        forwarded_chat_id=None,
        forwarded_channel_post=None,
        forwarded_date=None,
        forwarded_post_author=None,
        grouped_id=None,
        reply_to_msg_id=None,
        media_type="document",
        mime_type="application/zip",
        original_name="a.zip",
        file_size=10,
        width=None,
        height=None,
        duration=None,
        telegram_file_id="file-a",
    )
    db.record_source_message_metadata(
        source="channels",
        source_key="channel_200",
        source_chat_id=-100200,
        source_message_id=7,
        file_sha256=None,
        final_path="/tmp/b.zip",
        message_date=None,
        edit_date=None,
        text=None,
        forwarded_sender_id=None,
        forwarded_chat_id=None,
        forwarded_channel_post=None,
        forwarded_date=None,
        forwarded_post_author=None,
        grouped_id=None,
        reply_to_msg_id=None,
        media_type="document",
        mime_type="application/zip",
        original_name="b.zip",
        file_size=20,
        width=None,
        height=None,
        duration=None,
        telegram_file_id="file-b",
    )

    rows = db.connection.execute(
        """
        SELECT source_key, source_message_id, final_path
        FROM source_message_metadata
        ORDER BY source_key
        """
    ).fetchall()

    assert [(row["source_key"], row["source_message_id"], row["final_path"]) for row in rows] == [
        ("channel_100", 7, "/tmp/a.zip"),
        ("channel_200", 7, "/tmp/b.zip"),
    ]


def test_channel_failures_are_scoped_by_source_key(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    for source in ("channel_100", "channel_200"):
        db.record_source_archive_failure(
            source=source,
            source_message_id=7,
            media_type="document",
            original_name=f"{source}.zip",
            file_size=1,
            error_kind="network",
            error_class="TimeoutError",
            error_message="timeout",
            retryable=True,
            temp_path=f"/tmp/{source}.part",
        )

    failures = db.unresolved_source_archive_failures(limit=10)

    assert {failure.source for failure in failures} == {"channel_100", "channel_200"}
