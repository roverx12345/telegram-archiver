import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tele_bot.db import Database
from tele_bot.media import MediaRef
from tele_bot.saved_archiver import (
    ChannelCheckResult,
    ChannelInfo,
    SavedArchiverSettings,
    SavedStats,
    archive_message,
    classify_archive_exception,
    channel_partial_message_refs,
    channel_storage_folder_name,
    channel_storage_roots,
    download_media_resumable,
    format_channel_check_result,
    format_channel_list,
    format_saved_stats,
    has_protected_content,
    load_channel_archiver_settings,
    is_blocked_saved_message,
    message_in_date_range,
    media_ref_from_message,
    media_ref_matches_extensions,
    maybe_strip_archive_password,
    multipart_archive_part,
    normalized_channel_id,
    parse_channel_peer_id,
    parse_extension_set,
    parse_keyword_set,
    parse_peer_list,
    retry_partial_saved_messages,
    saved_partial_message_ids,
    resumable_temp_path,
    scan_existing_saved_messages,
    scan_recent_saved_messages,
)


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


def test_resumable_temp_path_can_be_scoped_to_channel(tmp_path: Path) -> None:
    ref = MediaRef(
        media_type="video",
        file_id="telegram-file-id",
        file_unique_id="unique",
        file_name="my clip.mp4",
        file_size=123,
        mime_type="video/mp4",
        extension=".mp4",
    )

    result = resumable_temp_path(tmp_path, 42, ref, source_key="channel_2683725559")

    assert result.name.startswith("channel_2683725559_42_")
    assert result.name.endswith("_my_clip.mp4.part")


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


def test_format_channel_list() -> None:
    text = format_channel_list(
        [
            ChannelInfo(
                id=123,
                title="Example Channel",
                username="example",
                broadcast=True,
                megagroup=False,
                protected_content=True,
            )
        ]
    )

    assert "count=1" in text
    assert "peer=-100123" in text
    assert "username=@example" in text
    assert "flags=broadcast,protected" in text
    assert "title=Example Channel" in text


def test_channel_storage_roots_use_stable_peer_id_folders(tmp_path: Path) -> None:
    first = type("Channel", (), {"id": 123, "title": "Example/Channel"})()
    duplicate_title = type("Channel", (), {"id": 456, "title": "Example/Channel"})()
    no_title = type("Channel", (), {"id": -100789, "title": None})()

    roots = channel_storage_roots(tmp_path, [first, duplicate_title, no_title])

    assert roots[123] == tmp_path / "peer-100123"
    assert roots[456] == tmp_path / "peer-100456"
    assert roots[100789] == tmp_path / "peer-100100789"


def test_channel_storage_folder_name_uses_full_peer_id() -> None:
    assert channel_storage_folder_name(3018373376) == "peer-1003018373376"


def test_parse_channel_peer_id_accepts_listed_and_full_ids() -> None:
    assert parse_channel_peer_id("2683725559") == 2683725559
    assert parse_channel_peer_id("-1002683725559") == 2683725559
    assert parse_channel_peer_id("@example") is None


def test_normalized_channel_id_accepts_full_and_internal_ids() -> None:
    assert normalized_channel_id(2683725559) == 2683725559
    assert normalized_channel_id(-1002683725559) == 2683725559
    assert normalized_channel_id(None) == 0


def test_parse_peer_list_trims_empty_items() -> None:
    assert parse_peer_list(" @one, , -1002683725559 ") == ("@one", "-1002683725559")


def test_parse_extension_set_supports_archives_alias() -> None:
    extensions = parse_extension_set("archives,.cbz, zip")

    assert ".zip" in extensions
    assert ".rar" in extensions
    assert ".cbz" in extensions


def test_media_ref_matches_multi_part_archive_extension() -> None:
    ref = MediaRef(
        media_type="document",
        file_id="file",
        file_unique_id="unique",
        file_name="bundle.tar.gz",
        file_size=1,
        mime_type="application/gzip",
        extension=".gz",
    )

    assert media_ref_matches_extensions(ref, frozenset({".tar.gz"})) is True


def test_media_ref_matches_split_archive_extensions_from_archives_alias() -> None:
    for name in ("bundle.part2.rar", "bundle.r00", "bundle.z01", "bundle.002"):
        ref = MediaRef(
            media_type="document",
            file_id="file",
            file_unique_id="unique",
            file_name=name,
            file_size=1,
            mime_type="application/octet-stream",
            extension=Path(name).suffix,
        )
        assert media_ref_matches_extensions(ref, frozenset({".zip", ".rar", ".7z", ".001"})) is True


def test_multipart_archive_part_detects_common_volume_names() -> None:
    assert multipart_archive_part("bundle.part1.rar").is_first is True
    assert multipart_archive_part("bundle.part2.rar").order == 2
    assert multipart_archive_part("bundle.r00").is_first is False
    assert multipart_archive_part("bundle.zip").requires_sibling is True
    assert multipart_archive_part("bundle.001").is_first is True


def test_maybe_strip_archive_password_repacks_encrypted_zip(tmp_path: Path) -> None:
    seven_zip = shutil.which("7z") or shutil.which("7zz")
    if seven_zip is None:
        pytest.skip("7z is not installed")

    source_file = tmp_path / "plain.txt"
    source_file.write_text("secret content", encoding="utf-8")
    encrypted_path = tmp_path / "encrypted.zip"
    password_file = tmp_path / "passwords.txt"
    password_file.write_text("wrong\nsecret\n", encoding="utf-8")
    subprocess.run(
        [seven_zip, "a", "-tzip", "-psecret", str(encrypted_path), str(source_file.name)],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    ref = MediaRef(
        media_type="document",
        file_id="file",
        file_unique_id="unique",
        file_name="encrypted.zip",
        file_size=encrypted_path.stat().st_size,
        mime_type="application/zip",
        extension=".zip",
    )

    unlocked = maybe_strip_archive_password(
        encrypted_path,
        ref,
        password_file=password_file,
        enabled=True,
    )
    unlocked_path = unlocked.path
    unlocked_ref = unlocked.ref

    assert unlocked_path != encrypted_path
    assert unlocked_path.name.endswith("_unlocked.zip")
    assert unlocked_ref.file_name == "encrypted_unlocked.zip"
    assert unlocked_ref.mime_type == "application/zip"
    assert unlocked.status == "unlocked"
    assert unlocked.password_matched is True
    assert unlocked.original_name == "encrypted.zip"
    assert unlocked.output_name == "encrypted_unlocked.zip"
    subprocess.run(
        [seven_zip, "t", "-y", str(unlocked_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def test_maybe_strip_archive_password_merges_split_zip(tmp_path: Path) -> None:
    seven_zip = shutil.which("7z") or shutil.which("7zz")
    if seven_zip is None:
        pytest.skip("7z is not installed")

    source_file = tmp_path / "plain.bin"
    source_file.write_bytes(os.urandom(20_000))
    password_file = tmp_path / "passwords.txt"
    password_file.write_text("wrong\nsecret\n", encoding="utf-8")
    subprocess.run(
        [seven_zip, "a", "-tzip", "-v1k", "-psecret", str(tmp_path / "split.zip"), str(source_file.name)],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    volume_paths = sorted(tmp_path.glob("split.zip.*"))
    if len(volume_paths) < 2:
        pytest.skip("7z did not create a multi-volume archive")

    first_ref = MediaRef(
        media_type="document",
        file_id="first",
        file_unique_id="channel_1:1:first",
        file_name="split.zip.001",
        file_size=volume_paths[0].stat().st_size,
        mime_type="application/octet-stream",
        extension=".001",
    )
    first_temp = resumable_temp_path(tmp_path, 1, first_ref, source_key="channel_1")
    volume_paths[0].replace(first_temp)
    second_temp: Path | None = None
    for index, volume_path in enumerate(volume_paths[1:], start=2):
        extension = Path(volume_path.name).suffix
        ref = MediaRef(
            media_type="document",
            file_id=f"volume-{index}",
            file_unique_id=f"channel_1:{index}:volume",
            file_name=volume_path.name,
            file_size=volume_path.stat().st_size,
            mime_type="application/octet-stream",
            extension=extension,
        )
        temp_path = resumable_temp_path(tmp_path, index, ref, source_key="channel_1")
        volume_path.replace(temp_path)
        if index == 2:
            second_temp = temp_path

    unlocked = maybe_strip_archive_password(
        first_temp,
        first_ref,
        password_file=password_file,
        enabled=True,
        temp_dir=tmp_path,
        source_key="channel_1",
    )

    assert unlocked.path != first_temp
    assert unlocked.path.name.endswith("_unlocked.zip")
    assert unlocked.status == "unlocked_multipart"
    assert unlocked.password_matched is True
    assert unlocked.part_group == "split.zip"
    assert unlocked.part_count == len(volume_paths)
    assert second_temp is not None
    assert second_temp in unlocked.cleanup_paths
    subprocess.run(
        [seven_zip, "t", "-y", str(unlocked.path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def test_load_channel_archiver_settings_try_protected_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("CHANNEL_ARCHIVE_PEERS", "-1003018373376")
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("TEXT_DOWNLOAD_DIR", str(tmp_path / "texts"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("CHANNEL_TELEGRAM_SESSION", str(tmp_path / "channel.session"))
    monkeypatch.setenv("CHANNEL_TRY_PROTECTED_CONTENT", "true")

    monkeypatch.setenv("CHANNEL_ARCHIVE_CONFIG", "")

    settings = load_channel_archiver_settings()

    assert settings.peers == ("-1003018373376",)
    assert settings.targets[0].try_protected_content is True


def test_load_channel_archiver_settings_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "peer": "-1003018373376",
                        "media_types": ["video", "animation"],
                        "extensions": [".mp4", ".mkv"],
                        "archive_text": False,
                        "try_protected_content": True,
                        "existing_limit": 100,
                        "recent_limit": 25,
                        "start_date": "2026-07-01",
                        "end_date": "2026-07-09T23:00:00+00:00",
                        "download_delay_seconds": 1.5,
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("CHANNEL_ARCHIVE_CONFIG", str(config_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("TEXT_DOWNLOAD_DIR", str(tmp_path / "texts"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("CHANNEL_TELEGRAM_SESSION", str(tmp_path / "channel.session"))

    settings = load_channel_archiver_settings()
    target = settings.targets[0]

    assert settings.peers == ("-1003018373376",)
    assert target.media_types == frozenset({"video", "animation"})
    assert target.allowed_extensions == frozenset({".mp4", ".mkv"})
    assert target.archive_text is False
    assert target.try_protected_content is True
    assert target.existing_scan_limit == 100
    assert target.recent_scan_limit == 25
    assert target.start_date == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert target.end_date == datetime(2026, 7, 9, 23, tzinfo=timezone.utc)
    assert target.download_delay_seconds == 1.5


def test_message_in_date_range_handles_naive_and_aware_dates() -> None:
    message = type("Message", (), {"date": datetime(2026, 7, 5, 12, 0, 0)})()

    assert message_in_date_range(
        message,
        start_date=datetime(2026, 7, 5, tzinfo=timezone.utc),
        end_date=datetime(2026, 7, 6, tzinfo=timezone.utc),
    ) is True
    assert message_in_date_range(message, start_date=datetime(2026, 7, 6, tzinfo=timezone.utc)) is False


def test_format_channel_check_result() -> None:
    text = format_channel_check_result(
        ChannelCheckResult(
            peer="@example",
            id=123,
            title="Example Channel",
            username="example",
            protected_content=True,
            scanned_messages=20,
            media_messages=4,
            protected_messages=2,
            sample_download_status="skipped_protected",
            sample_download_detail="protected content flag found",
        )
    )

    assert "peer=@example" in text
    assert "protected_content=True" in text
    assert "media_messages=4" in text
    assert "sample_download_status=skipped_protected" in text


def test_classify_archive_exception_marks_file_reference_retryable() -> None:
    exc_type = type("FileReferenceExpiredError", (Exception,), {})

    result = classify_archive_exception(exc_type("expired"))

    assert result.kind == "expired_file_reference"
    assert result.retryable is True
    assert "refetching" in result.message


def test_has_protected_content_accepts_telethon_flag_names() -> None:
    assert has_protected_content(type("Entity", (), {"noforwards": True})()) is True
    assert has_protected_content(type("Entity", (), {"no_forwards": True})()) is True


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


class PartialThenCompleteClient:
    def __init__(self) -> None:
        self.offsets: list[int] = []

    async def iter_download(self, message: object, *, offset: int, file_size: int | None):
        self.offsets.append(offset)
        if offset == 0:
            yield b"part"
        elif offset == len(b"part"):
            yield b"ial"


@pytest.mark.anyio
async def test_resumable_download_retries_incomplete_progress(tmp_path: Path) -> None:
    temp_path = tmp_path / "media.part"
    client = PartialThenCompleteClient()
    message = type("Message", (), {"id": 456})()
    ref = MediaRef(
        media_type="document",
        file_id="file",
        file_unique_id="unique",
        file_name="media.bin",
        file_size=len(b"partial"),
        mime_type="application/octet-stream",
        extension=".bin",
    )

    result = await download_media_resumable(client, message, ref, temp_path)

    assert result == temp_path
    assert temp_path.read_bytes() == b"partial"
    assert client.offsets == [0, len(b"part")]


class IncompleteThenFallbackClient:
    def __init__(self) -> None:
        self.offsets: list[int] = []
        self.fallback_called = False

    async def iter_download(self, message: object, *, offset: int, file_size: int | None):
        self.offsets.append(offset)
        if offset == 0:
            yield b"bad"

    async def download_media(self, message: object, *, file: str):
        self.fallback_called = True
        Path(file).write_bytes(b"complete")
        return file


@pytest.mark.anyio
async def test_resumable_download_falls_back_to_full_download(tmp_path: Path) -> None:
    temp_path = tmp_path / "media.part"
    client = IncompleteThenFallbackClient()
    message = type("Message", (), {"id": 789})()
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
    assert client.fallback_called is True



def saved_settings(tmp_path: Path, *, scan_progress_every: int = 1000) -> SavedArchiverSettings:
    return SavedArchiverSettings(
        api_id=1,
        api_hash="hash",
        session_path=tmp_path / "session",
        download_dir=tmp_path / "downloads",
        text_dir=tmp_path / "texts",
        db_path=tmp_path / "bot.db",
        log_level="INFO",
        archive_existing=True,
        scan_progress_every=scan_progress_every,
        recent_scan_interval_seconds=900,
        recent_scan_limit=2000,
        retry_partials_on_start=True,
        retry_partials_limit=0,
        blocked_forward_chat_ids=frozenset(),
        blocked_forward_keywords=frozenset(),
        proxy=None,
    )


class IterMessagesClient:
    def __init__(self, messages: list[object]) -> None:
        self.messages = messages
        self.calls: list[dict[str, object]] = []

    async def iter_messages(self, chat: str, **kwargs):
        assert chat == "me"
        self.calls.append(kwargs)
        for message in self.messages:
            yield message


@pytest.mark.anyio
async def test_scan_existing_saved_messages_logs_progress(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    messages = [type("Message", (), {"id": item, "text": "", "raw_text": ""})() for item in range(3)]
    database = Database(tmp_path / "bot.db")
    settings = saved_settings(tmp_path, scan_progress_every=2)

    await scan_existing_saved_messages(IterMessagesClient(messages), database, settings)

    assert "Saved Messages scan progress: scanned=2" in caplog.text
    assert "Existing Saved Messages scan finished: scanned=3" in caplog.text


@pytest.mark.anyio
async def test_scan_recent_saved_messages_uses_limit(tmp_path: Path) -> None:
    messages = [type("Message", (), {"id": item, "text": "", "raw_text": ""})() for item in range(3)]
    database = Database(tmp_path / "bot.db")
    settings = saved_settings(tmp_path)
    client = IterMessagesClient(messages)

    await scan_recent_saved_messages(client, database, settings, limit=2)

    assert client.calls == [{"limit": 2}]


def test_media_ref_from_message_skips_webpage_preview() -> None:
    message = type(
        "Message",
        (),
        {
            "id": 402131,
            "media": type("MessageMediaWebPage", (), {})(),
            "file": None,
            "document": None,
            "photo": None,
        },
    )()

    assert media_ref_from_message(message) is None


def test_media_ref_from_message_uses_source_key_for_unique_id() -> None:
    message = type(
        "Message",
        (),
        {
            "id": 42,
            "media": object(),
            "file": type("File", (), {"name": "media.bin", "size": 10, "mime_type": "application/octet-stream"})(),
            "document": type("Document", (), {"id": "file-id"})(),
            "photo": None,
        },
    )()

    saved_ref = media_ref_from_message(message)
    channel_ref = media_ref_from_message(message, source_key="channel_2683725559")

    assert saved_ref is not None
    assert channel_ref is not None
    assert saved_ref.file_unique_id == "saved:42:file-id"
    assert channel_ref.file_unique_id == "channel_2683725559:42:file-id"


def test_saved_partial_message_ids_extracts_unique_message_ids_by_mtime(tmp_path: Path) -> None:
    first = tmp_path / "saved_20_a_video.mp4.part"
    duplicate = tmp_path / "saved_20_b_video.mp4.part"
    second = tmp_path / "saved_30_c_video.mp4.part"
    ignored = tmp_path / "other.part"
    for path in [first, duplicate, second, ignored]:
        path.write_bytes(b"")
    first.touch()
    duplicate.touch()
    second.touch()

    assert saved_partial_message_ids(tmp_path) == [20, 30]
    assert saved_partial_message_ids(tmp_path, limit=1) == [20]


def test_channel_partial_message_refs_extracts_channel_and_message_ids(tmp_path: Path) -> None:
    first = tmp_path / "channel_2683725559_20_a_video.mp4.part"
    duplicate = tmp_path / "channel_2683725559_20_b_video.mp4.part"
    second = tmp_path / "channel_2683725559_30_c_video.mp4.part"
    other_channel = tmp_path / "channel_123_20_c_video.mp4.part"
    ignored = tmp_path / "saved_20_c_video.mp4.part"
    for index, path in enumerate([first, duplicate, second, other_channel, ignored], start=1):
        path.write_bytes(b"")
        os.utime(path, (index, index))

    assert channel_partial_message_refs(tmp_path) == [(2683725559, 20), (2683725559, 30), (123, 20)]
    assert channel_partial_message_refs(tmp_path, limit=1) == [(2683725559, 20)]


class PartialRetryClient:
    def __init__(self, messages: list[object]) -> None:
        self.messages = messages
        self.requested_ids: list[list[int]] = []

    async def get_messages(self, chat: str, *, ids: list[int]):
        assert chat == "me"
        self.requested_ids.append(ids)
        by_id = {message.id: message for message in self.messages}
        return [by_id.get(message_id) for message_id in ids]


@pytest.mark.anyio
async def test_retry_partial_saved_messages_reprocesses_partial_message_ids(tmp_path: Path) -> None:
    settings = saved_settings(tmp_path)
    temp_dir = settings.download_dir / ".tmp"
    temp_dir.mkdir(parents=True)
    (temp_dir / "saved_42_a_video.mp4.part").write_bytes(b"partial")
    message = type("Message", (), {"id": 42, "text": "", "raw_text": ""})()
    client = PartialRetryClient([message])
    seen: list[int] = []

    async def archive_one(item: object) -> None:
        seen.append(item.id)

    await retry_partial_saved_messages(client, Database(tmp_path / "bot.db"), settings, archive_one=archive_one)

    assert client.requested_ids == [[42]]
    assert seen == [42]


class DownloadCompleteClient:
    async def iter_download(self, message: object, *, offset: int, file_size: int | None):
        yield b"complete"


class FailingRecordDatabase(Database):
    def record_saved_file(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("database failed")


@pytest.mark.anyio
async def test_archive_message_moves_final_file_back_to_part_when_database_fails(tmp_path: Path) -> None:
    database = FailingRecordDatabase(tmp_path / "bot.db")
    settings = saved_settings(tmp_path)
    message = type("Message", (), {"id": 123, "chat_id": 1, "text": "", "raw_text": ""})()
    ref = MediaRef(
        media_type="document",
        file_id="file",
        file_unique_id="saved:123:file",
        file_name="media.bin",
        file_size=len(b"complete"),
        mime_type="application/octet-stream",
        extension=".bin",
    )
    message.media = object()
    message.file = type(
        "File",
        (),
        {"name": "media.bin", "size": len(b"complete"), "mime_type": "application/octet-stream"},
    )()
    message.document = type(
        "Document",
        (),
        {
            "id": "file",
            "file_reference": b"ref",
            "size": len(b"complete"),
            "mime_type": "application/octet-stream",
            "attributes": [],
        },
    )()

    await archive_message(DownloadCompleteClient(), database, settings, message)

    temp_path = resumable_temp_path(settings.download_dir / ".tmp", message.id, ref)
    assert temp_path.read_bytes() == b"complete"
    assert list((settings.download_dir / "documents").glob("*.bin")) == []


@pytest.mark.anyio
async def test_archive_message_can_store_media_under_channel_root(tmp_path: Path) -> None:
    database = Database(tmp_path / "bot.db")
    settings = saved_settings(tmp_path)
    message = type("Message", (), {"id": 123, "chat_id": -100456, "text": "", "raw_text": ""})()
    message.media = object()
    message.file = type(
        "File",
        (),
        {"name": "media.bin", "size": len(b"complete"), "mime_type": "application/octet-stream"},
    )()
    message.document = type(
        "Document",
        (),
        {
            "id": "file",
            "file_reference": b"ref",
            "size": len(b"complete"),
            "mime_type": "application/octet-stream",
            "attributes": [],
        },
    )()

    await archive_message(
        DownloadCompleteClient(),
        database,
        settings,
        message,
        source_key="channel_456",
        media_root_dir=tmp_path / "downloads" / "Example Channel",
    )

    assert list((settings.download_dir / "documents").glob("*.bin")) == []
    files = list((settings.download_dir / "Example Channel" / "documents").glob("*.bin"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"complete"


@pytest.mark.anyio
async def test_archive_message_skips_media_outside_allowed_media_types(tmp_path: Path) -> None:
    database = Database(tmp_path / "bot.db")
    settings = saved_settings(tmp_path)
    message = type("Message", (), {"id": 123, "chat_id": -100456, "text": "", "raw_text": ""})()
    message.media = object()
    message.file = type(
        "File",
        (),
        {"name": "media.bin", "size": len(b"complete"), "mime_type": "application/octet-stream"},
    )()
    message.document = type(
        "Document",
        (),
        {
            "id": "file",
            "file_reference": b"ref",
            "size": len(b"complete"),
            "mime_type": "application/octet-stream",
            "attributes": [],
        },
    )()

    await archive_message(
        DownloadCompleteClient(),
        database,
        settings,
        message,
        allowed_media_types=frozenset({"video"}),
    )

    assert list((settings.download_dir / "documents").glob("*.bin")) == []


@pytest.mark.anyio
async def test_archive_message_records_archive_processing_metadata(tmp_path: Path) -> None:
    database = Database(tmp_path / "bot.db")
    settings = saved_settings(tmp_path)
    message = type("Message", (), {"id": 124, "chat_id": -100456, "text": "", "raw_text": ""})()
    message.media = object()
    message.file = type(
        "File",
        (),
        {"name": "media.zip", "size": len(b"complete"), "mime_type": "application/zip"},
    )()
    message.document = type(
        "Document",
        (),
        {
            "id": "zip-file",
            "file_reference": b"ref",
            "size": len(b"complete"),
            "mime_type": "application/zip",
            "attributes": [],
        },
    )()

    await archive_message(
        DownloadCompleteClient(),
        database,
        settings,
        message,
        source_key="channel_456",
        media_root_dir=tmp_path / "downloads" / "Example Channel",
    )

    record = database.get_archive_processing("channel_456", 124)

    assert record is not None
    assert record.status == "not_requested"
    assert record.password_matched is False
    assert record.original_name == "media.zip"
    assert record.final_path is not None
    assert record.file_sha256 is not None



def test_parse_keyword_set_normalizes_items() -> None:
    assert parse_keyword_set(" ZYjia, other ") == frozenset({"zyjia", "other"})


def test_blocked_saved_message_matches_forward_chat_id(tmp_path: Path) -> None:
    settings = saved_settings(tmp_path)
    settings = SavedArchiverSettings(
        **{**settings.__dict__, "blocked_forward_chat_ids": frozenset({-1001188449201})}
    )
    message = type("Message", (), {"id": 1, "forward": type("Forward", (), {"chat_id": -1001188449201})()})()

    assert is_blocked_saved_message(settings, message) is True


def test_blocked_saved_message_does_not_match_keyword_in_filename(tmp_path: Path) -> None:
    settings = saved_settings(tmp_path)
    settings = SavedArchiverSettings(
        **{**settings.__dict__, "blocked_forward_keywords": frozenset({"zyjia"})}
    )
    message = type("Message", (), {"id": 1, "forward": None, "raw_text": ""})()
    ref = MediaRef(
        media_type="video",
        file_id="file",
        file_unique_id="unique",
        file_name="电报群搜@ZYjia1.mp4",
        file_size=1,
        mime_type="video/mp4",
        extension=".mp4",
    )

    assert is_blocked_saved_message(settings, message, ref) is False



def test_blocked_saved_message_matches_keyword_in_forward_chat_username(tmp_path: Path) -> None:
    settings = saved_settings(tmp_path)
    settings = SavedArchiverSettings(
        **{**settings.__dict__, "blocked_forward_keywords": frozenset({"zyjia"})}
    )
    chat = type("Chat", (), {"title": "Some channel", "username": "ZYjia_archive"})()
    forward = type("Forward", (), {"chat_id": -1001, "chat": chat, "sender_id": None, "sender": None})()
    message = type("Message", (), {"id": 1, "forward": forward, "raw_text": ""})()

    assert is_blocked_saved_message(settings, message) is True
