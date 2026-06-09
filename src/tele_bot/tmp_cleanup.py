from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TmpCleanupCandidate:
    path: Path
    size: int
    age_seconds: float


@dataclass(frozen=True)
class TmpCleanupResult:
    tmp_dir: Path
    older_than_seconds: int
    dry_run: bool
    candidates: tuple[TmpCleanupCandidate, ...]
    deleted_count: int = 0
    deleted_bytes: int = 0


def collect_tmp_cleanup_candidates(
    download_dir: Path,
    *,
    older_than_seconds: int,
    now: float | None = None,
) -> tuple[TmpCleanupCandidate, ...]:
    tmp_dir = download_dir / ".tmp"
    if not tmp_dir.exists():
        return ()

    cutoff_now = time.time() if now is None else now
    candidates: list[TmpCleanupCandidate] = []
    for path in sorted(tmp_dir.glob("*.part")):
        if not path.is_file():
            continue
        stat = path.stat()
        age_seconds = cutoff_now - stat.st_mtime
        if age_seconds >= older_than_seconds:
            candidates.append(TmpCleanupCandidate(path=path, size=stat.st_size, age_seconds=age_seconds))
    return tuple(candidates)


def clean_tmp_part_files(
    download_dir: Path,
    *,
    older_than_seconds: int,
    dry_run: bool = True,
    now: float | None = None,
) -> TmpCleanupResult:
    tmp_dir = download_dir / ".tmp"
    candidates = collect_tmp_cleanup_candidates(download_dir, older_than_seconds=older_than_seconds, now=now)
    deleted_count = 0
    deleted_bytes = 0

    if not dry_run:
        for candidate in candidates:
            try:
                candidate.path.unlink()
            except FileNotFoundError:
                continue
            deleted_count += 1
            deleted_bytes += candidate.size

    return TmpCleanupResult(
        tmp_dir=tmp_dir,
        older_than_seconds=older_than_seconds,
        dry_run=dry_run,
        candidates=candidates,
        deleted_count=deleted_count,
        deleted_bytes=deleted_bytes,
    )


def format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{size} B"


def format_tmp_cleanup_result(result: TmpCleanupResult) -> str:
    total_bytes = sum(candidate.size for candidate in result.candidates)
    action = "would_delete" if result.dry_run else "deleted"
    lines = [
        "Temporary partial file cleanup",
        f"tmp_dir={result.tmp_dir}",
        f"older_than_seconds={result.older_than_seconds}",
        f"dry_run={result.dry_run}",
        f"candidates={len(result.candidates)}",
        f"candidate_bytes={total_bytes}",
        f"candidate_size={format_bytes(total_bytes)}",
        f"{action}_count={result.deleted_count if not result.dry_run else len(result.candidates)}",
        f"{action}_bytes={result.deleted_bytes if not result.dry_run else total_bytes}",
    ]
    return "\n".join(lines)
