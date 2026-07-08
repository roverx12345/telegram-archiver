# Operations Guide

## Pair and test

1. Open a private chat with the bot.
2. Run `/pair <your-pair-code>`.
3. Forward a media message to the bot.

## Source services

- `telegram-archiver bot` runs the forward-to-bot source.
- `telegram-archiver saved` runs the Saved Messages source.
- `telegram-archiver dashboard` runs a read-only local HTML monitor for non-bot sources. It reports Saved Messages and channel archive totals, recent archives, and `.part` download state from the shared database and download directory.
- `telegram-archiver health` prints the same non-bot state as a terminal report, including unresolved Saved Messages and channel failures.
- `telegram-archiver saved-stats` scans Saved Messages and prints download candidate counts without downloading. Use `--limit N` for a quick sample. Stop the running `saved-archiver` service first if it uses the same Telethon session.
- `telegram-archiver clean-tmp --older-than-days 30` prints a dry-run summary of stale `DOWNLOAD_DIR/.tmp/*.part` files. Add `--delete` to remove them after stopping active download services. Use `--download-dir PATH` when running on the host with a Docker-only `DOWNLOAD_DIR`.
- `telegram-archiver all` runs both sources under one supervisor.

## Useful commands

- `/status` shows counts for saved files and current chat jobs.
- `/jobs` shows recent jobs for the current private chat.
- `/failed` shows failed jobs and whether they are still retryable.
- `/retry_failed` triggers retry for retryable failed jobs in the current chat.

## Recovery behavior

- Pending Telegram updates are preserved across restarts.
- Download jobs are persisted in SQLite.
- Failed jobs can be retried automatically on restart or manually with `/retry_failed`.
- The bot source re-downloads failed files from the beginning.
- The Saved Messages source resumes interrupted media downloads from `DOWNLOAD_DIR/.tmp/*.part`.
- Saved Messages and channel failures are written to SQLite as unresolved failures and marked resolved when the same message later archives successfully.
