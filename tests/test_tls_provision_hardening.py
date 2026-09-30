"""CA repair after key-only corruption, and cert content/constraint checks."""
import shutil
import subprocess

import pytest

from plexus import tls

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


def _pubkey_of_key(path):
    return subprocess.run(
        ["openssl", "pkey", "-in", str(path), "-pubout"],
        capture_output=True, text=True,
    ).stdout


def _pubkey_of_cert(path):
    return subprocess.run(
        ["openssl", "x509", "-in", str(path), "-pubkey", "-noout"],
        capture_output=True, text=True,
    ).stdout


def _text(path):
    return subprocess.run(
        ["openssl", "x509", "-in", str(path), "-noout", "-text"],
        capture_output=True, text=True,
    ).stdout


def test_repair_after_key_only_corruption(tmp_path):
    tls.provision(tmp_path)
    ca_key_before = (tmp_path / "ca.key").read_bytes()
    ca_pem_before = (tmp_path / "ca.pem").read_bytes()

    # Overwrite ca.key with a fresh, unrelated EC key -- ca.pem stays put,
    # so the pair no longer shares a public key.
    subprocess.run(
        ["openssl", "ecparam", "-genkey", "-name", "prime256v1", "-noout",
         "-out", str(tmp_path / "ca.key")],
        capture_output=True, text=True, check=True,
    )
    assert not tls._ca_pair_matches(tmp_path)

    tls.provision(tmp_path)

    ca_key_after = (tmp_path / "ca.key").read_bytes()
    ca_pem_after = (tmp_path / "ca.pem").read_bytes()
    assert ca_key_after != ca_key_before
    assert ca_pem_after != ca_pem_before
    assert tls._ca_pair_matches(tmp_path)
    assert _pubkey_of_key(tmp_path / "ca.key") == _pubkey_of_cert(tmp_path / "ca.pem")

    verify = subprocess.run(
        ["openssl", "verify", "-CAfile", str(tmp_path / "ca.pem"), str(tmp_path / "proxy.pem")],
        capture_output=True, text=True,
    )
    assert verify.returncode == 0

    # A second, uncorrupted call is still a no-op.
    ca_pem_repaired = (tmp_path / "ca.pem").read_bytes()
    tls.provision(tmp_path)
    assert (tmp_path / "ca.pem").read_bytes() == ca_pem_repaired


def test_ca_and_proxy_cert_content(tmp_path):
    tls.provision(tmp_path)
    ca_text = _text(tmp_path / "ca.pem")

    assert "CA:TRUE" in ca_text
    assert "pathlen:0" in ca_text
    assert "Certificate Sign" in ca_text
    assert "CRL Sign" in ca_text

    # name constraints print across a couple of lines; grab the block instead of one line.
    nc_start = ca_text.index("X509v3 Name Constraints")
    nc_block = ca_text[nc_start:nc_start + 300]
    permitted_dns = [
        line.strip().removeprefix("DNS:")
        for line in nc_block.splitlines()
        if line.strip().startswith("DNS:")
    ]
    assert sorted(permitted_dns) == ["egress", "egress-web"]

    verify = subprocess.run(
        ["openssl", "verify", "-CAfile", str(tmp_path / "ca.pem"), str(tmp_path / "proxy.pem")],
        capture_output=True, text=True,
    )
    assert verify.returncode == 0

    proxy_text = _text(tmp_path / "proxy.pem")
    assert "DNS:egress" in proxy_text
    assert "DNS:egress-web" in proxy_text

    # A cert for a third name, signed by the same CA, must fail the CA's
    # name constraints even though the signature itself is valid.
    evil_key = tmp_path / "evil.key"
    evil_csr = tmp_path / "evil.csr"
    evil_pem = tmp_path / "evil.pem"
    evil_ext = tmp_path / "evil.ext"
    evil_ext.write_text("subjectAltName=DNS:evil.example\n")

    subprocess.run(
        ["openssl", "ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", str(evil_key)],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["openssl", "req", "-new", "-key", str(evil_key), "-subj", "/CN=evil", "-out", str(evil_csr)],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["openssl", "x509", "-req", "-in", str(evil_csr),
         "-CA", str(tmp_path / "ca.pem"), "-CAkey", str(tmp_path / "ca.key"),
         "-CAcreateserial", "-days", "365", "-extfile", str(evil_ext), "-out", str(evil_pem)],
        capture_output=True, text=True, check=True,
    )

    evil_verify = subprocess.run(
        ["openssl", "verify", "-CAfile", str(tmp_path / "ca.pem"), str(evil_pem)],
        capture_output=True, text=True,
    )
    assert evil_verify.returncode != 0
