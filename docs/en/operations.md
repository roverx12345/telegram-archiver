# Operations Guide

## Pair and test

1. Open a private chat with the bot.
2. Run `/pair <your-pair-code>`.
3. Forward a media message to the bot.

## Source services

- `telegram-archiver bot` runs the forward-to-bot source.
- `telegram-archiver saved` runs the Saved Messages source.
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
