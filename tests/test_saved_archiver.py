from pathlib import Path

from tele_bot.media import MediaRef
from tele_bot.saved_archiver import SavedStats, format_saved_stats, resumable_temp_path


def test_resumable_temp_path_is_stable_and_part_suffixed(tmp_path: Path) -> None:
    ref = MediaRef(
        media_type="video",
        file_id="telegram-file-id",
        file_unique_id="unique",
        file_name="my clip.mp4",
        file_size=123,
        mime_type="video/mp4",
        extension=".mp4",
    )

    first = resumable_temp_path(tmp_path, 42, ref)
    second = resumable_temp_path(tmp_path, 42, ref)

    assert first == second
    assert first.name.startswith("saved_42_")
    assert first.name.endswith("_my_clip.mp4.part")


def test_format_saved_stats() -> None:
    text = format_saved_stats(
        SavedStats(
            scanned_messages=10,
            media_messages=7,
            blocked_media=1,
            already_archived_by_unique_id=2,
            already_archived_by_file_id=1,
            download_candidates=3,
        )
    )

    assert "scanned_messages=10" in text
    assert "media_messages=7" in text
    assert "blocked_media=1" in text
    assert "already_archived=3" in text
    assert "download_candidates=3" in text
