# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

- Added English and Chinese documentation split.
- Added Linux `systemd` deployment template and bootstrap script.
- Added GitHub Actions CI workflow.

## 0.1.0

- Added forwarded media download and save flow.
- Added two-layer dedupe using `file_unique_id` and `sha256`.
- Added pairing-based authorization.
- Added pending update recovery after bot restarts.
- Added persistent download jobs with retry and restart recovery.
- Added `/status`, `/jobs`, `/failed`, and `/retry_failed` commands.
