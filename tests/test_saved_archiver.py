from pathlib import Path

from tele_bot.media import MediaRef
from tele_bot.saved_archiver import resumable_temp_path


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
