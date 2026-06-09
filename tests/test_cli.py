from pathlib import Path

import pytest

from tele_bot.cli import main


def test_cli_help_lists_sources(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "bot" in output
    assert "saved" in output
    assert "saved-stats" in output
    assert "clean-tmp" in output
    assert "all" in output


def test_saved_stats_help_lists_limit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["saved-stats", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--limit" in output
    assert "--progress-every" in output


def test_clean_tmp_help_lists_delete(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["clean-tmp", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--older-than-days" in output
    assert "--download-dir" in output
    assert "--delete" in output


def test_clean_tmp_uses_download_dir_without_bot_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))

    main(["clean-tmp", "--older-than-days", "30"])

    output = capsys.readouterr().out
    assert f"tmp_dir={tmp_path / '.tmp'}" in output
    assert "dry_run=True" in output
