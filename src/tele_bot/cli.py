from __future__ import annotations

import argparse
import asyncio
import logging
import multiprocessing
import time
from pathlib import Path

from .bot import build_application
from .config import load_download_dir, load_settings
from .dashboard import run_dashboard, run_health
from .db import Database
from .saved_archiver import run_archiver, run_channel_archiver, run_channel_check, run_channels_list, run_saved_stats
from .tmp_cleanup import clean_tmp_part_files, format_tmp_cleanup_result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="telegram-archiver",
        description="Archive Telegram media from multiple sources.",
    )
    subparsers = parser.add_subparsers(dest="source")
    subparsers.add_parser("bot", help="Run the forward-to-bot source.")
    subparsers.add_parser("saved", help="Run the Saved Messages source.")
    subparsers.add_parser("channels", help="Run the configured channel archiver source.")
    subparsers.add_parser("dashboard", help="Run the read-only HTML monitor for non-bot sources.")
    subparsers.add_parser("health", help="Print a read-only health summary for non-bot archive state.")
    saved_stats_parser = subparsers.add_parser("saved-stats", help="Scan Saved Messages and print download candidate counts.")
    saved_stats_parser.add_argument("--limit", type=int, default=None, help="Only scan the newest N Saved Messages.")
    saved_stats_parser.add_argument("--progress-every", type=int, default=1000, help="Log progress every N scanned messages.")
    channels_list_parser = subparsers.add_parser("channels-list", help="List joined Telegram channels visible to the user session.")
    channels_list_parser.add_argument("--include-groups", action="store_true", help="Also include channel-backed megagroups.")
    channels_list_parser.add_argument("--limit", type=int, default=None, help="Stop after listing N matching channels.")
    channel_check_parser = subparsers.add_parser("channel-check", help="Check one channel for accessibility, media, and protected-content flags.")
    channel_check_parser.add_argument("peer", help="Channel username, invite-resolved name, or numeric peer id.")
    channel_check_parser.add_argument("--limit", type=int, default=20, help="Scan the newest N messages.")
    channel_check_parser.add_argument("--download-sample", action="store_true", help="Try one temporary media download when no protection flag is found.")
    channel_check_parser.add_argument("--max-sample-mb", type=int, default=50, help="Skip sample download if the media is larger than this many MiB.")
    clean_tmp_parser = subparsers.add_parser("clean-tmp", help="Clean stale resumable .part files from DOWNLOAD_DIR/.tmp.")
    clean_tmp_parser.add_argument("--older-than-days", type=float, default=30.0, help="Only clean .part files older than this many days.")
    clean_tmp_parser.add_argument("--download-dir", type=Path, default=None, help="Override DOWNLOAD_DIR, useful when running on the host instead of Docker.")
    clean_tmp_parser.add_argument("--delete", action="store_true", help="Actually delete files. Without this flag, only print a dry-run summary.")
    subparsers.add_parser("all", help="Run both sources under one supervisor.")
    args = parser.parse_args(argv)

    source = args.source or "bot"
    if source == "bot":
        run_bot_source()
    elif source == "saved":
        run_saved_source()
    elif source == "channels":
        run_channels_source()
    elif source == "dashboard":
        run_dashboard()
    elif source == "health":
        raise SystemExit(run_health())
    elif source == "saved-stats":
        asyncio.run(run_saved_stats(limit=args.limit, progress_every=args.progress_every))
    elif source == "channels-list":
        asyncio.run(run_channels_list(include_groups=args.include_groups, limit=args.limit))
    elif source == "channel-check":
        if args.limit <= 0:
            raise SystemExit("--limit must be greater than 0")
        if args.max_sample_mb <= 0:
            raise SystemExit("--max-sample-mb must be greater than 0")
        asyncio.run(
            run_channel_check(
                args.peer,
                limit=args.limit,
                download_sample=args.download_sample,
                max_sample_bytes=args.max_sample_mb * 1024 * 1024,
            )
        )
    elif source == "clean-tmp":
        run_tmp_cleanup(older_than_days=args.older_than_days, download_dir=args.download_dir, delete=args.delete)
    elif source == "all":
        run_all_sources()
    else:  # pragma: no cover - argparse prevents this path.
        parser.error(f"unknown source: {source}")


def run_bot_source() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database = Database(settings.db_path)
    application = build_application(settings, database)
    try:
        application.run_polling(drop_pending_updates=False)
    finally:
        database.close()


def run_saved_source() -> None:
    asyncio.run(run_archiver())


def run_channels_source() -> None:
    asyncio.run(run_channel_archiver())


def run_tmp_cleanup(*, older_than_days: float, download_dir: Path | None, delete: bool) -> None:
    if older_than_days < 0:
        raise SystemExit("--older-than-days must be greater than or equal to 0")

    resolved_download_dir = download_dir.expanduser().resolve() if download_dir is not None else load_download_dir()
    older_than_seconds = int(older_than_days * 24 * 60 * 60)
    result = clean_tmp_part_files(
        resolved_download_dir,
        older_than_seconds=older_than_seconds,
        dry_run=not delete,
    )
    print(format_tmp_cleanup_result(result))


def run_all_sources() -> None:
    processes = [
        multiprocessing.Process(target=run_bot_source, name="telegram-archiver-bot"),
        multiprocessing.Process(target=run_saved_source, name="telegram-archiver-saved"),
    ]
    for process in processes:
        process.start()

    interrupted = False
    terminated_by_supervisor: set[int] = set()
    try:
        while all(process.is_alive() for process in processes):
            time.sleep(1)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                if process.pid is not None:
                    terminated_by_supervisor.add(process.pid)
        for process in processes:
            process.join()

    if interrupted:
        return

    failures = [
        process
        for process in processes
        if process.exitcode not in (0, None) and process.pid not in terminated_by_supervisor
    ]
    if failures:
        names = ", ".join(f"{process.name}={process.exitcode}" for process in failures)
        raise SystemExit(f"archiver source exited with failure: {names}")


if __name__ == "__main__":
    main()
