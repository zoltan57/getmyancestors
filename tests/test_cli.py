from __future__ import annotations

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

    parser = cli.build_parser()
    args = parser.parse_args(["fetch"])

    assert args.db == "env.sqlite"
    assert args.username == "env-user"
    assert args.password == "env-pass"
    assert args.rate_limit == 5
    assert args.timeout == 30
    assert args.ascend == 2
    assert args.descend == 1


def test_explicit_cli_flag_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_DB", "env.sqlite")
    monkeypatch.setenv("FS_USERNAME", "env-user")

    parser = cli.build_parser()
    args = parser.parse_args(["fetch", "--db", "cli.sqlite", "-u", "cli-user"])

    assert args.db == "cli.sqlite"
    assert args.username == "cli-user"


def test_missing_db_without_env_returns_exit_code_2(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["load"])
    assert exit_code == 2
    assert "FS_DB" in capsys.readouterr().err


def test_missing_username_for_fetch_without_env_returns_exit_code_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["fetch", "--db", "somewhere.sqlite"])
    assert exit_code == 2
    assert "FS_USERNAME" in capsys.readouterr().err


def test_env_var_supplies_db_for_load_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
