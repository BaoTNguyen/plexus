"""The autouse conftest fixture keeps every test's events out of the real spool."""
import os
import uuid
from pathlib import Path

from plexus import events


def test_journal_dir_is_under_pytest_tmp(tmp_path_factory):
    journal = Path(os.environ["EVENT_JOURNAL_DIR"]).resolve()
    assert journal.is_relative_to(tmp_path_factory.getbasetemp().resolve())


def test_emit_lands_in_fixture_journal_not_home_spool(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("VASCULAR_HOME", str(home))
    marker = uuid.uuid4().hex
    events.emit("journal.isolation_check", goal_id="g1", marker=marker)
    journal = Path(os.environ["EVENT_JOURNAL_DIR"])
    assert any(marker in f.read_text() for f in journal.rglob("*") if f.is_file())
    assert not (home / "spool" / "events").exists()
