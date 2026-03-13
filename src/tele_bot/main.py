from __future__ import annotations

import logging

from .bot import build_application
from .config import load_settings
from .db import Database


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database = Database(settings.db_path)
    application = build_application(settings, database)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
