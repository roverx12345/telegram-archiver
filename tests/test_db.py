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
