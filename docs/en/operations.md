# Operations Guide

## Pair and test

1. Open a private chat with the bot.
2. Run `/pair <your-pair-code>`.
3. Forward a media message to the bot.

## Useful commands

- `/status` shows counts for saved files and current chat jobs.
- `/jobs` shows recent jobs for the current private chat.
- `/failed` shows failed jobs and whether they are still retryable.
- `/retry_failed` triggers retry for retryable failed jobs in the current chat.

## Recovery behavior

- Pending Telegram updates are preserved across restarts.
- Download jobs are persisted in SQLite.
- Failed jobs can be retried automatically on restart or manually with `/retry_failed`.
- Recovery re-downloads the file; it is not a resume-from-offset implementation.
