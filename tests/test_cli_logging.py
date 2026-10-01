"""The command's logging (talos.cli._logging, _rotate): talos.log stays in bounds with several
processes writing it, and under launchd stderr carries only warnings and errors."""

import logging

from talos import cli


def test_a_log_past_its_limit_is_moved_aside_keeping_the_newest_old_ones(tmp_path):
    log = tmp_path / "talos.log"
    for i in range(1, 4):
        (tmp_path / f"talos.log.{i}").write_text(f"old {i}")
    log.write_text("x" * 20)
    assert not cli._rotate(log, keep=3, limit=100)               # under the limit: left alone
    assert cli._rotate(log, keep=3, limit=10)
    assert not log.exists()
    assert (tmp_path / "talos.log.1").read_text() == "x" * 20
    assert (tmp_path / "talos.log.2").read_text() == "old 1"
    assert (tmp_path / "talos.log.3").read_text() == "old 2"       # "old 3" was the oldest: gone
    assert not (tmp_path / "talos.log.4").exists()


def test_under_launchd_stderr_gets_warnings_only_and_the_file_everything(tmp_path, monkeypatch, capsys):
    from talos.config import Settings
    root = logging.getLogger()
    saved = list(root.handlers)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    try:
        cli._logging(Settings(home=tmp_path, dsn="x"), verbose=False)
        logging.getLogger("talos.test").info("folder had nothing new")
        logging.getLogger("talos.test").warning("sync failed")
        for h in root.handlers:
            h.flush()
    finally:
        for h in root.handlers[len(saved):]:
            h.close()
        root.handlers[:] = saved
    err = capsys.readouterr().err
    assert "sync failed" in err and "nothing new" not in err
    text = (tmp_path / "logs" / "talos.log").read_text()
    assert "sync failed" in text and "nothing new" in text
