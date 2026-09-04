from __future__ import annotations

import logging
from pathlib import Path

from getmyancestors.logging_config import configure_logging, default_logfile_path


def test_configure_logging_verbose_controls_console_output(capsys) -> None:
    logger = logging.getLogger("getmyancestors.somefakemodule")

    configure_logging(verbose=True, logfile=None)
    logger.debug("debug-visible")
    assert "debug-visible" in capsys.readouterr().err

    configure_logging(verbose=False, logfile=None)
    logger.debug("debug-hidden")
    logger.warning("warning-visible")
    stderr = capsys.readouterr().err
    assert "debug-hidden" not in stderr
    assert "warning-visible" in stderr


def test_configure_logging_logfile_always_receives_debug(tmp_path: Path, capsys) -> None:
    logfile = tmp_path / "x.log"
    logger = logging.getLogger("getmyancestors.somefakemodule")

    configure_logging(verbose=False, logfile=logfile)
    logger.debug("debug-to-file")

    assert "debug-to-file" not in capsys.readouterr().err
    assert "debug-to-file" in logfile.read_text(encoding="utf-8")


def test_configure_logging_replaces_handlers_without_duplication(capsys) -> None:
    logger = logging.getLogger("getmyancestors.somefakemodule")

    configure_logging(verbose=True, logfile=None)
    logger.warning("warning-1")
    assert capsys.readouterr().err.count("warning-1") == 1

    configure_logging(verbose=True, logfile=None)
    logger.warning("warning-2")
    assert capsys.readouterr().err.count("warning-2") == 1


def test_default_logfile_path_uses_db_directory_and_expected_name(tmp_path: Path) -> None:
    db_path = tmp_path / "family.sqlite"
    logfile = default_logfile_path(db_path)

    assert logfile.parent == tmp_path
    assert logfile.name.startswith("family.sqlite.")
    assert logfile.name.endswith(".log")
