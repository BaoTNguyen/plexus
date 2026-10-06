"""CA pair repair: a crash mid-write or a hand-made mismatch never wedges provision()."""
import shutil

import pytest

from plexus import tls

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


def test_interrupted_after_key_leaves_no_mismatched_pair(tmp_path, monkeypatch):
    real_make_ca = tls._make_ca

    def crash_after_key(d):
        real_write_via_tmp = tls._write_via_tmp
        calls = []

        def spy(dir, name, mode, run):
            real_write_via_tmp(dir, name, mode, run)
            calls.append(name)
            if name == "ca.key":
                raise RuntimeError("simulated crash before ca.pem")

        monkeypatch.setattr(tls, "_write_via_tmp", spy)
        real_make_ca(d)

    monkeypatch.setattr(tls, "_make_ca", crash_after_key)
    with pytest.raises(RuntimeError):
        tls.provision(tmp_path)

    # d itself must not hold a lone ca.key -- either nothing, or a repaired pair.
    assert not (tmp_path / "ca.key").is_file()
    assert not (tmp_path / "ca.pem").is_file()

    monkeypatch.undo()
    tls.provision(tmp_path)
    assert tls._ca_pair_matches(tmp_path)
    assert (tmp_path / "proxy.pem").is_file()


def test_mismatched_pair_regenerated_with_proxy(tmp_path):
    tls.provision(tmp_path)
    proxy_before = (tmp_path / "proxy.pem").read_bytes()

    # Clobber ca.pem with a cert from an unrelated key -- key/cert no longer match.
    other_key = tmp_path / "other.key"
    other_pem = tmp_path / "other.pem"
    subprocess_run = tls._openssl
    subprocess_run("ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", str(other_key))
    subprocess_run(
        "req", "-x509", "-new", "-key", str(other_key), "-days", "1",
        "-subj", "/CN=impostor", "-out", str(other_pem),
    )
    (tmp_path / "ca.pem").write_bytes(other_pem.read_bytes())

    assert not tls._ca_pair_matches(tmp_path)

    tls.provision(tmp_path)

    assert tls._ca_pair_matches(tmp_path)
    assert (tmp_path / "proxy.pem").read_bytes() != proxy_before


def test_valid_pair_left_untouched(tmp_path):
    tls.provision(tmp_path)
    ca_key_before = (tmp_path / "ca.key").read_bytes()
    ca_pem_before = (tmp_path / "ca.pem").read_bytes()
    proxy_before = (tmp_path / "proxy.pem").read_bytes()

    tls.provision(tmp_path)

    assert (tmp_path / "ca.key").read_bytes() == ca_key_before
    assert (tmp_path / "ca.pem").read_bytes() == ca_pem_before
    assert (tmp_path / "proxy.pem").read_bytes() == proxy_before
