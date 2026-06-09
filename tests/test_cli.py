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
    assert "all" in output


def test_saved_stats_help_lists_limit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["saved-stats", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--limit" in output
    assert "--progress-every" in output
