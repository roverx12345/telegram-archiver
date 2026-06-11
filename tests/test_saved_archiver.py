from pathlib import Path

import pytest

from tele_bot.media import MediaRef
from tele_bot.saved_archiver import SavedStats, download_media_resumable, format_saved_stats, resumable_temp_path


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


class NoProgressThenCompleteClient:
    def __init__(self) -> None:
        self.offsets: list[int] = []

    async def iter_download(self, message: object, *, offset: int, file_size: int | None):
        self.offsets.append(offset)
        if offset:
            if False:
                yield b""
            return
        yield b"complete"


@pytest.mark.anyio
async def test_resumable_download_restarts_when_partial_makes_no_progress(tmp_path: Path) -> None:
    temp_path = tmp_path / "media.part"
    temp_path.write_bytes(b"stuck")
    client = NoProgressThenCompleteClient()
    message = type("Message", (), {"id": 123})()
    ref = MediaRef(
        media_type="document",
        file_id="file",
        file_unique_id="unique",
        file_name="media.bin",
        file_size=len(b"complete"),
        mime_type="application/octet-stream",
        extension=".bin",
    )

    result = await download_media_resumable(client, message, ref, temp_path)

    assert result == temp_path
    assert temp_path.read_bytes() == b"complete"
    assert client.offsets == [len(b"stuck"), 0]
