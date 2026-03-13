from pathlib import Path

from tele_bot.bot import build_help_text, bot_commands
from tele_bot.config import Settings


def settings(pair_code: str | None = "secret", allow_unpaired_private: bool = False) -> Settings:
    return Settings(
        bot_token="123:ABC",
        pair_code=pair_code,
        download_dir=Path("downloads"),
        db_path=Path("data/bot.db"),
        local_bot_api_url=None,
        log_level="INFO",
        allow_unpaired_private=allow_unpaired_private,
        max_download_retries=3,
    )


def test_bot_commands_include_pair_when_auth_required() -> None:
    commands = bot_commands(settings())
    names = [command.command for command in commands]
    assert "start" in names
    assert "help" in names
    assert "pair" in names
    assert "retry_failed" in names


def test_bot_commands_skip_pair_when_unpaired_private_allowed() -> None:
    commands = bot_commands(settings(pair_code=None, allow_unpaired_private=True))
    names = [command.command for command in commands]
    assert "pair" not in names


def test_help_text_contains_doc_links() -> None:
    text = build_help_text(settings(), include_intro=False)
    assert "English README:" in text
    assert "中文 README:" in text
    assert "Linux 教程:" in text
