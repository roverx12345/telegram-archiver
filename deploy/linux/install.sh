#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/roverx12345/telegram-archiver.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/telegram-archiver}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "Installing Telegram Archiver"
echo "Repository: ${REPO_URL}"
echo "Install dir: ${INSTALL_DIR}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python not found: ${PYTHON_BIN}" >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required" >&2
  exit 1
fi

if [ ! -d "${INSTALL_DIR}/.git" ]; then
  git clone "${REPO_URL}" "${INSTALL_DIR}"
else
  git -C "${INSTALL_DIR}" pull --ff-only
fi

cd "${INSTALL_DIR}"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

"${PYTHON_BIN}" -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -e '.[dev]'

echo
echo "Bootstrap complete."
echo "Next steps:"
echo "1. Edit ${INSTALL_DIR}/.env"
echo "2. Start manually with: ${INSTALL_DIR}/.venv/bin/telegram-archiver bot"
echo "3. Or install deploy/systemd/telegram-archiver-bot.service"
