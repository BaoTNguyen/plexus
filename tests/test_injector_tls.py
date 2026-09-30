"""CA and proxy certs: provisioned once, reported honestly, left alone when fresh."""
import shutil
import time

import pytest

from plexus import registry, tls

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


def test_status_missing_before_provision(tmp_path):
    out = tls.status(tmp_path)
    assert out == ["tls ca: missing", "tls proxy: missing"]


def test_provision_creates_ca_and_proxy_cert(tmp_path):
    tls.provision(tmp_path)
    assert (tmp_path / "ca.key").is_file()
    assert (tmp_path / "ca.pem").is_file()
    assert (tmp_path / "proxy.key").is_file()
    assert (tmp_path / "proxy.pem").is_file()
    assert oct((tmp_path / "ca.key").stat().st_mode)[-3:] == "600"
    assert oct((tmp_path / "proxy.key").stat().st_mode)[-3:] == "600"

    out = tls.status(tmp_path)
    assert out[0].startswith("tls ca: ok")
    assert out[1].startswith("tls proxy: ok")


def test_provision_is_idempotent(tmp_path):
    tls.provision(tmp_path)
    ca_before = (tmp_path / "ca.pem").read_bytes()
    proxy_before = (tmp_path / "proxy.pem").read_bytes()
    mtime_before = (tmp_path / "ca.key").stat().st_mtime_ns

    time.sleep(0.01)
    tls.provision(tmp_path)

    assert (tmp_path / "ca.pem").read_bytes() == ca_before
    assert (tmp_path / "proxy.pem").read_bytes() == proxy_before
    assert (tmp_path / "ca.key").stat().st_mtime_ns == mtime_before


def test_provision_renews_expiring_proxy_cert_without_touching_ca(tmp_path):
    tls.provision(tmp_path)
    ca_before = (tmp_path / "ca.pem").read_bytes()

    # Force the proxy cert to look expired without regenerating the CA.
    import plexus.tls as tls_mod

    real_days_left = tls_mod._days_left

    def fake_days_left(cert):
        if cert.name == "proxy.pem":
            return 1.0
        return real_days_left(cert)

    tls_mod._days_left = fake_days_left
    try:
        tls.provision(tmp_path)
    finally:
        tls_mod._days_left = real_days_left

    assert (tmp_path / "ca.pem").read_bytes() == ca_before
    assert "tls proxy: ok" in tls.status(tmp_path)[1]


def test_seat_env_sets_tls_vars_when_chatgpt_seat_has_ca(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "injected_seats", lambda: ["chatgpt"])
    monkeypatch.setattr(registry, "seat_secrets", lambda: tmp_path)
    monkeypatch.setattr(registry, "codex_claims", lambda: {})
    monkeypatch.delenv("HEART_SANDBOX_INJECT_TLS_PORT", raising=False)
    monkeypatch.delenv("HEART_SANDBOX_HOME_FILES", raising=False)
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    (tls_dir / "ca.pem").write_text("ca")

    env = registry.seat_env()

    assert env["HEART_SANDBOX_INJECT_TLS_PORT"] == "8890"
    assert env["HEART_SANDBOX_CA_CERT"] == str(tls_dir / "ca.pem")


def test_seat_env_omits_tls_vars_when_ca_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "injected_seats", lambda: ["chatgpt"])
    monkeypatch.setattr(registry, "seat_secrets", lambda: tmp_path)
    monkeypatch.setattr(registry, "codex_claims", lambda: {})
    monkeypatch.delenv("HEART_SANDBOX_HOME_FILES", raising=False)

    env = registry.seat_env()

    assert "HEART_SANDBOX_INJECT_TLS_PORT" not in env
    assert "HEART_SANDBOX_CA_CERT" not in env


def test_seat_env_never_leaks_a_key_path(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "injected_seats", lambda: ["chatgpt"])
    monkeypatch.setattr(registry, "seat_secrets", lambda: tmp_path)
    monkeypatch.setattr(registry, "codex_claims", lambda: {})
    monkeypatch.delenv("HEART_SANDBOX_HOME_FILES", raising=False)
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    (tls_dir / "ca.pem").write_text("ca")
    (tls_dir / "ca.key").write_text("key")
    (tls_dir / "proxy.key").write_text("key")

    env = registry.seat_env()

    assert not any(str(v).endswith(".key") for v in env.values())
