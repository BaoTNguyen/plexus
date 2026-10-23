"""PLEXUS-B14: only docker-sbx or an explicit `off` may run agents or their checks."""
from plexus.sandbox import gate


def test_only_docker_sbx_or_off_passes(monkeypatch):
    monkeypatch.delenv("HEART_SANDBOX", raising=False)
    assert "unset" in gate()
    for bad in ("", "1", "docker"):
        monkeypatch.setenv("HEART_SANDBOX", bad)
        assert gate(), bad
    for ok in ("docker-sbx", "off"):
        monkeypatch.setenv("HEART_SANDBOX", ok)
        assert gate() is None, ok
