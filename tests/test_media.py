from pathlib import Path

from tele_bot.media import MediaRef, build_storage_name, sanitize_filename


def test_sanitize_filename() -> None:
    assert sanitize_filename(" a/b:c?.mp4 ") == "a_b_c_.mp4"


def test_build_storage_name_keeps_extension() -> None:
    ref = MediaRef(
        media_type="video",
        file_id="f1",
        file_unique_id="u1",
        file_name="my clip.mp4",
        file_size=123,
        mime_type="video/mp4",
        extension=".mp4",
    )
    assert build_storage_name(ref, "a" * 64) == "my_clip__aaaaaaaaaaaa.mp4"


def test_build_storage_name_without_original_name() -> None:
    ref = MediaRef(
        media_type="photo",
        file_id="f1",
        file_unique_id="u1",
        file_name=None,
        file_size=123,
        mime_type="image/jpeg",
        extension=".jpg",
    )
    assert build_storage_name(ref, "b" * 64) == "photo__bbbbbbbbbbbb.jpg"
