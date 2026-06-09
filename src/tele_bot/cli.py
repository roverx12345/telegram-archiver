from __future__ import annotations

import argparse
import asyncio
import logging
import multiprocessing
import time

from .bot import build_application
from .config import load_settings
from .db import Database
from .saved_archiver import run_archiver, run_saved_stats


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="telegram-archiver",
        description="Archive Telegram media from multiple sources.",
    )
    subparsers = parser.add_subparsers(dest="source")
    subparsers.add_parser("bot", help="Run the forward-to-bot source.")
    subparsers.add_parser("saved", help="Run the Saved Messages source.")
    saved_stats_parser = subparsers.add_parser("saved-stats", help="Scan Saved Messages and print download candidate counts.")
    saved_stats_parser.add_argument("--limit", type=int, default=None, help="Only scan the newest N Saved Messages.")
    saved_stats_parser.add_argument("--progress-every", type=int, default=1000, help="Log progress every N scanned messages.")
    subparsers.add_parser("all", help="Run both sources under one supervisor.")
    args = parser.parse_args(argv)

    source = args.source or "bot"
    if source == "bot":
        run_bot_source()
    elif source == "saved":
        run_saved_source()
    elif source == "saved-stats":
        asyncio.run(run_saved_stats(limit=args.limit, progress_every=args.progress_every))
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


def run_all_sources() -> None:
    processes = [
        multiprocessing.Process(target=run_bot_source, name="telegram-archiver-bot"),
        multiprocessing.Process(target=run_saved_source, name="telegram-archiver-saved"),
    ]
    for process in processes:
        process.start()

    try:
        while all(process.is_alive() for process in processes):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join()

    failures = [process for process in processes if process.exitcode not in (0, None)]
    if failures:
        names = ", ".join(f"{process.name}={process.exitcode}" for process in failures)
        raise SystemExit(f"archiver source exited with failure: {names}")


if __name__ == "__main__":
    main()
