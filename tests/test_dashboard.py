import os
from pathlib import Path

from tele_bot.dashboard import (
    DashboardSettings,
    collect_dashboard_status,
    collect_partial_summary,
    format_health_report,
    partial_source,
    render_dashboard_html,
)
from tele_bot.db import Database


def test_dashboard_counts_non_bot_archives_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    db.record_saved_file(
        sha256="saved-sha",
        final_path=str(tmp_path / "downloads" / "videos" / "saved.mp4"),
        original_name="saved.mp4",
        media_type="video",
        mime_type="video/mp4",
        file_size=100,
        source_chat_id=1,
        source_message_id=10,
        forwarded_from=None,
        telegram_file_unique_id="saved:10:123",
        telegram_file_id="123",
    )
    db.record_saved_file(
        sha256="channel-sha",
        final_path=str(tmp_path / "downloads" / "documents" / "channel.zip"),
        original_name="channel.zip",
        media_type="document",
        mime_type="application/zip",
        file_size=300,
        source_chat_id=2,
        source_message_id=20,
        forwarded_from=None,
        telegram_file_unique_id="channel_2683725559:20:456",
        telegram_file_id="456",
    )
    db.record_saved_file(
        sha256="bot-sha",
        final_path=str(tmp_path / "downloads" / "photos" / "bot.jpg"),
        original_name="bot.jpg",
        media_type="photo",
        mime_type="image/jpeg",
        file_size=700,
        source_chat_id=3,
        source_message_id=30,
        forwarded_from=None,
        telegram_file_unique_id="bot-file-unique-id",
        telegram_file_id="bot-file-id",
    )
    db.record_source_archive_failure(
        source="saved",
        source_message_id=40,
        media_type="video",
        original_name="broken.mp4",
        file_size=999,
        error_kind="expired_file_reference",
        error_class="FileReferenceExpiredError",
        error_message="expired",
        retryable=True,
        temp_path=str(tmp_path / "downloads" / ".tmp" / "broken.part"),
    )
    db.close()

    settings = DashboardSettings(
        host="127.0.0.1",
        port=8765,
        db_path=tmp_path / "bot.db",
        download_dir=tmp_path / "downloads",
        refresh_seconds=10,
        active_partial_seconds=600,
        stale_partial_days=30,
    )

    status = collect_dashboard_status(settings)

    assert status["database"]["available"] is True
    assert status["database"]["totals"] == {"files": 2, "bytes": 400}
    assert {item["source"]: item["files"] for item in status["database"]["sources"]} == {
        "channels": 1,
        "saved": 1,
    }
    assert {item["media_type"]: item["files"] for item in status["database"]["media_types"]} == {
        "document": 1,
        "video": 1,
    }
    assert status["database"]["failures"]["unresolved_count"] == 1
    assert status["database"]["failures"]["retryable_count"] == 1
    assert status["database"]["failures"]["recent"][0]["error_kind"] == "expired_file_reference"
    assert status["health"]["level"] == "warning"


def test_partial_summary_tracks_active_and_stale_files(tmp_path: Path) -> None:
    tmp_dir = tmp_path / ".tmp"
    tmp_dir.mkdir()
    active = tmp_dir / "saved_42_file.mp4.part"
    stale = tmp_dir / "channel_123_44_file.zip.part"
    active.write_bytes(b"a" * 10)
    stale.write_bytes(b"b" * 20)
    os.utime(active, (1_000, 1_000))
    os.utime(stale, (100, 100))

    summary = collect_partial_summary(
        tmp_dir,
        now=1_050,
        active_partial_seconds=100,
        stale_partial_days=0,
    )

    assert summary["count"] == 2
    assert summary["bytes"] == 30
    assert summary["active_count"] == 1
    assert summary["active_bytes"] == 10
    assert {item["source"]: item["files"] for item in summary["by_source"]} == {
        "channels": 1,
        "saved": 1,
    }


def test_dashboard_html_contains_api_polling() -> None:
    html = render_dashboard_html(
        DashboardSettings(
            host="127.0.0.1",
            port=8765,
            db_path=Path("bot.db"),
            download_dir=Path("downloads"),
            refresh_seconds=7,
            active_partial_seconds=600,
            stale_partial_days=30,
        )
    )

    assert "/api/status" in html
    assert "Non-bot sources" in html
    assert "Unresolved Failures" in html
    assert "refresh <span id=\"refresh\">7</span>s" in html


def test_partial_source_classifies_known_prefixes() -> None:
    assert partial_source("saved_1_video.mp4.part") == "saved"
    assert partial_source("channel_123_1_bundle.zip.part") == "channels"
    assert partial_source("other.part") == "unknown"


def test_format_health_report_includes_failures(tmp_path: Path) -> None:
    settings = DashboardSettings(
        host="127.0.0.1",
        port=8765,
        db_path=tmp_path / "missing.db",
        download_dir=tmp_path / "downloads",
        refresh_seconds=10,
        active_partial_seconds=600,
        stale_partial_days=30,
    )
    status = collect_dashboard_status(settings)

    text = format_health_report(status)

    assert "Telegram Archiver health" in text
    assert "db_available=False" in text
    assert "unresolved_failures=0" in text
