import os
from pathlib import Path

from tele_bot.tmp_cleanup import clean_tmp_part_files, collect_tmp_cleanup_candidates, format_tmp_cleanup_result


def test_collect_tmp_cleanup_candidates_only_matches_stale_part_files(tmp_path: Path) -> None:
    tmp_dir = tmp_path / ".tmp"
    tmp_dir.mkdir()
    stale_part = tmp_dir / "old.part"
    fresh_part = tmp_dir / "fresh.part"
    stale_other = tmp_dir / "old.tmp"
    stale_part.write_bytes(b"old")
    fresh_part.write_bytes(b"fresh")
    stale_other.write_bytes(b"ignore")

    now = 1_700_000_000.0
    os.utime(stale_part, (now - 10_000, now - 10_000))
    os.utime(fresh_part, (now - 10, now - 10))
    os.utime(stale_other, (now - 10_000, now - 10_000))

    candidates = collect_tmp_cleanup_candidates(tmp_path, older_than_seconds=3600, now=now)

    assert [candidate.path for candidate in candidates] == [stale_part]
    assert candidates[0].size == 3


def test_clean_tmp_part_files_dry_run_keeps_candidates(tmp_path: Path) -> None:
    tmp_dir = tmp_path / ".tmp"
    tmp_dir.mkdir()
    stale_part = tmp_dir / "old.part"
    stale_part.write_bytes(b"old")

    result = clean_tmp_part_files(tmp_path, older_than_seconds=0, dry_run=True)

    assert stale_part.exists()
    assert result.deleted_count == 0
    assert "dry_run=True" in format_tmp_cleanup_result(result)
    assert "would_delete_count=1" in format_tmp_cleanup_result(result)


def test_clean_tmp_part_files_delete_removes_candidates(tmp_path: Path) -> None:
    tmp_dir = tmp_path / ".tmp"
    tmp_dir.mkdir()
    stale_part = tmp_dir / "old.part"
    stale_part.write_bytes(b"old")

    result = clean_tmp_part_files(tmp_path, older_than_seconds=0, dry_run=False)

    assert not stale_part.exists()
    assert result.deleted_count == 1
    assert result.deleted_bytes == 3
