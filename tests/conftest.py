import pytest


@pytest.fixture(autouse=True)
def _isolated_event_journal(tmp_path_factory, monkeypatch):
    # Overrides any inherited or import-time value so tests never append to ~/.vascular's events spool.
    monkeypatch.setenv("EVENT_JOURNAL_DIR", str(tmp_path_factory.getbasetemp() / "journal"))
    # heart reads pulse's socket with no silent file fallback (B11), and no
    # collector serves this tmp journal: read its files on purpose
    monkeypatch.setenv("HEART_PULSE", "file")
