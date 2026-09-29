"""CA and proxy certs: provisioned once, reported honestly, left alone when fresh."""
import shutil
import time

import pytest

from plexus import tls

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
