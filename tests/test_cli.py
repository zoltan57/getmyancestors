from __future__ import annotations

import logging
from pathlib import Path

import pytest

from getmyancestors import cli
from getmyancestors.db import connect, init_schema

FS_ENV_VARS = (
    "FS_DB",
    "FS_USERNAME",
    "FS_PASSWORD",
    "FS_RATE_LIMIT",
    "FS_TIMEOUT",
    "FS_ASCEND",
    "FS_DESCEND",
    "FS_MAX_PERSONS",
    "FS_NO_SOURCES",
    "FS_NO_NOTES",
    "FS_NO_MEMORIES",
)


@pytest.fixture(autouse=True)
def _no_dotenv_and_clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent a real .env file and ambient FS_* env vars from affecting tests."""
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)
    for name in FS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_env_vars_supply_defaults_for_fetch_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_DB", "env.sqlite")
    monkeypatch.setenv("FS_USERNAME", "env-user")
    monkeypatch.setenv("FS_PASSWORD", "env-pass")
    monkeypatch.setenv("FS_RATE_LIMIT", "5")
    monkeypatch.setenv("FS_TIMEOUT", "30")
    monkeypatch.setenv("FS_ASCEND", "2")
    monkeypatch.setenv("FS_DESCEND", "1")
    monkeypatch.setenv("FS_MAX_PERSONS", "180")

    parser = cli.build_parser()
    args = parser.parse_args(["fetch"])

    assert args.db == "env.sqlite"
    assert args.username == "env-user"
    assert args.password == "env-pass"
    assert args.rate_limit == 5
    assert args.timeout == 30
    assert args.ascend == 2
    assert args.descend == 1
    assert args.max_persons == 180


def test_explicit_cli_flag_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_DB", "env.sqlite")
    monkeypatch.setenv("FS_USERNAME", "env-user")

    parser = cli.build_parser()
    args = parser.parse_args(["fetch", "--db", "cli.sqlite", "-u", "cli-user"])

    assert args.db == "cli.sqlite"
    assert args.username == "cli-user"


def test_max_persons_above_200_is_capped_and_warned(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="getmyancestors.cli"):
        value = cli._cap_max_persons(250)

    assert value == 200
    assert "--max-persons 250 exceeds FamilySearch's documented maximum of 200; using 200 instead." in caplog.text


def test_max_persons_below_200_is_honored_without_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="getmyancestors.cli"):
        value = cli._cap_max_persons(150)

    assert value == 150
    assert "--max-persons" not in caplog.text


def test_fetch_optional_content_env_defaults_can_be_overridden_by_cli_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FS_NO_SOURCES", "1")
    monkeypatch.setenv("FS_NO_NOTES", "true")
    monkeypatch.setenv("FS_NO_MEMORIES", "yes")

    parser = cli.build_parser()
    default_args = parser.parse_args(["fetch"])
    assert default_args.no_sources is True
    assert default_args.no_notes is True
    assert default_args.no_memories is True

    override_args = parser.parse_args(["fetch", "--no-no-sources", "--no-no-notes", "--no-no-memories"])
    assert override_args.no_sources is False
    assert override_args.no_notes is False
    assert override_args.no_memories is False


def test_missing_db_without_env_returns_exit_code_2(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["load"])
    assert exit_code == 2
    assert "FS_DB" in capsys.readouterr().err


def test_missing_username_for_fetch_without_env_returns_exit_code_2(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    exit_code = cli.main(["fetch", "--db", str(tmp_path / "somewhere.sqlite")])
    assert exit_code == 2
    assert "FS_USERNAME" in capsys.readouterr().err


def test_env_var_supplies_db_for_load_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "env-load.sqlite"
    connection = connect(db_path)
    init_schema(connection)
    connection.close()

    monkeypatch.setenv("FS_DB", str(db_path))

    exit_code = cli.main(["load"])

    # No finished fetch run exists yet, so load() raises a ValueError that the
    # generic handler reports as exit code 1 -- proving --db came from FS_DB
    # rather than tripping the "missing --db" exit-code-2 path.
    assert exit_code == 1


def test_invalid_int_env_var_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FS_RATE_LIMIT", "not-a-number")

    exit_code = cli.main(["load", "--db", "somewhere.sqlite"])

    assert exit_code == 2
    assert "FS_RATE_LIMIT" in capsys.readouterr().err


def test_fetch_without_logfile_creates_auto_logfile_even_on_validation_error(tmp_path: Path) -> None:
    db_path = tmp_path / "family.sqlite"

    exit_code = cli.main(["fetch", "--db", str(db_path)])

    assert exit_code == 2
    logfiles = list(tmp_path.glob("family.sqlite.*.log"))
    assert len(logfiles) == 1
    assert logfiles[0].read_text(encoding="utf-8")


def test_fetch_with_explicit_logfile_overrides_auto_default(tmp_path: Path) -> None:
    db_path = tmp_path / "family.sqlite"
    custom_log = tmp_path / "custom.log"

    exit_code = cli.main(["fetch", "--db", str(db_path), "--logfile", str(custom_log)])

    assert exit_code == 2
    assert custom_log.exists()
    assert custom_log.read_text(encoding="utf-8")
    assert list(tmp_path.glob("family.sqlite.*.log")) == []


def test_repeated_main_calls_do_not_duplicate_stderr_messages(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    args = ["fetch", "--db", str(tmp_path / "family.sqlite")]

    first_code = cli.main(args)
    assert first_code == 2
    capsys.readouterr()

    second_code = cli.main(args)
    assert second_code == 2
    second_err = capsys.readouterr().err
    assert second_err.count("FS_USERNAME") == 1
