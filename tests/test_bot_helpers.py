from pathlib import Path

import pytest

from tele_bot.bot import bot_commands, build_help_text, require_authorized_private_chat
from tele_bot.db import Database
from tele_bot.config import Settings


def settings(pair_code: str | None = "secret", allow_unpaired_private: bool = False) -> Settings:
    return Settings(
        bot_token="123:ABC",
        pair_code=pair_code,
        download_dir=Path("downloads"),
        db_path=Path("data/bot.db"),
        local_bot_api_url=None,
        bot_proxy_url=None,
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



class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str | None]] = []

    async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
        self.replies.append((text, parse_mode))


class FakeChat:
    id = 99
    type = "private"


class FakeUser:
    id = 42


class FakeUpdate:
    def __init__(self) -> None:
        self.effective_message = FakeMessage()
        self.effective_chat = FakeChat()
        self.effective_user = FakeUser()


class FakeContext:
    def __init__(self, database: Database) -> None:
        self.application = type(
            "Application",
            (),
            {"bot_data": {"settings": settings(), "db": database}},
        )()


@pytest.mark.anyio
async def test_require_authorized_private_chat_blocks_unpaired_chat(tmp_path: Path) -> None:
    database = Database(tmp_path / "bot.db")
    update = FakeUpdate()
    context = FakeContext(database)

    message, chat, user = await require_authorized_private_chat(update, context)

    assert message is update.effective_message
    assert chat is None
    assert user is None
    assert update.effective_message.replies == [("当前会话尚未配对，请先发送 `/pair <配对码>`。", "Markdown")]
