"""INJECT_TLS_PORT: only offered to the proxy once its own cert exists, and
only for the ChatGPT route that needs it -- never a promise the proxy can't
back."""
import pytest

from plexus import sandbox


def _chatgpt_injected(monkeypatch):
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: ["chatgpt"])


def test_no_seat_means_no_tls_port(monkeypatch, tmp_path):
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: [])
    monkeypatch.setattr("plexus.registry.seat_secrets", lambda: tmp_path)
    want = sandbox._wanted_config("")
    assert want["INJECT_TLS_PORT"] == ""


def test_chatgpt_seat_without_certs_still_gets_no_tls_port(monkeypatch, tmp_path):
    _chatgpt_injected(monkeypatch)
    monkeypatch.setattr("plexus.registry.seat_secrets", lambda: tmp_path)
    want = sandbox._wanted_config("")
    assert want["INJECT_TLS_PORT"] == ""


def test_chatgpt_seat_with_partial_certs_still_gets_no_tls_port(monkeypatch, tmp_path):
    _chatgpt_injected(monkeypatch)
    monkeypatch.setattr("plexus.registry.seat_secrets", lambda: tmp_path)
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    (tls_dir / "ca.pem").write_text("ca")
    (tls_dir / "proxy.pem").write_text("leaf")
    # proxy.key missing -- a leaf with no key is nothing a server can use
    want = sandbox._wanted_config("")
    assert want["INJECT_TLS_PORT"] == ""


def test_chatgpt_seat_with_full_cert_set_gets_the_port(monkeypatch, tmp_path):
    _chatgpt_injected(monkeypatch)
    monkeypatch.setattr("plexus.registry.seat_secrets", lambda: tmp_path)
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    (tls_dir / "ca.pem").write_text("ca")
    (tls_dir / "proxy.pem").write_text("leaf")
    (tls_dir / "proxy.key").write_text("key")
    want = sandbox._wanted_config("")
    assert want["INJECT_TLS_PORT"] == sandbox._inject_tls_port()


def test_anthropic_only_seat_gets_no_tls_port_even_with_certs(monkeypatch, tmp_path):
    """The web/api lanes' credential injector serves the ChatGPT route only --
    an Anthropic-only seat has nothing that needs TLS from this proxy."""
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: ["anthropic"])
    monkeypatch.setattr("plexus.registry.seat_secrets", lambda: tmp_path)
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    (tls_dir / "ca.pem").write_text("ca")
    (tls_dir / "proxy.pem").write_text("leaf")
    (tls_dir / "proxy.key").write_text("key")
    want = sandbox._wanted_config("")
    assert want["INJECT_TLS_PORT"] == ""


def test_port_is_read_at_call_time_not_import_time(monkeypatch, tmp_path):
    """A module-level constant baked in at import would leak a monkeypatched
    env var into whatever runs after -- this reads the env on every call."""
    monkeypatch.delenv("HEART_SANDBOX_INJECT_TLS_PORT", raising=False)
    assert sandbox._inject_tls_port() == "8890"
    monkeypatch.setenv("HEART_SANDBOX_INJECT_TLS_PORT", "9999")
    assert sandbox._inject_tls_port() == "9999"
    monkeypatch.delenv("HEART_SANDBOX_INJECT_TLS_PORT")
    assert sandbox._inject_tls_port() == "8890"


def test_running_config_reports_stale_when_tls_port_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_docker", lambda *a, **k: (
        0, "true\t/secrets \t" + "\n".join(["ALLOW=x", "INJECT_PORT=8889"])))
    have = sandbox._running_config("egress")
    assert have["INJECT_TLS_PORT"] == ""


def test_start_proxy_passes_inject_tls_port_when_wanted(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sandbox, "_docker", lambda *a, **k: calls.append(a) or (0, ""))
    monkeypatch.setattr("plexus.registry.seat_secrets", lambda: tmp_path)
    monkeypatch.setattr("plexus.registry.sentinel_seed", lambda: tmp_path / "sentinel")
    want = {"ALLOW": "", "DENY": "", "INJECT_PORT": "8889", "INJECT_TLS_PORT": "8890",
            "secrets": True, "codex": True, "log": False}
    sandbox._start_proxy("egress", sandbox.NETWORK, want, tmp_path / "proxy.py")
    run = next(c for c in calls if c[0] == "run")
    assert "INJECT_TLS_PORT=8890" in run


def test_start_proxy_omits_inject_tls_port_when_not_wanted(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sandbox, "_docker", lambda *a, **k: calls.append(a) or (0, ""))
    monkeypatch.setattr("plexus.registry.seat_secrets", lambda: tmp_path)
    monkeypatch.setattr("plexus.registry.sentinel_seed", lambda: tmp_path / "sentinel")
    want = {"ALLOW": "", "DENY": "", "INJECT_PORT": "", "INJECT_TLS_PORT": "",
            "secrets": False, "codex": False, "log": False}
    sandbox._start_proxy("egress", sandbox.NETWORK, want, tmp_path / "proxy.py")
    run = next(c for c in calls if c[0] == "run")
    assert not any(a.startswith("INJECT_TLS_PORT=") for a in run)


def test_doctor_reports_tls_status_even_without_docker(monkeypatch, tmp_path):
    """doctor() returns early once docker is missing -- the TLS lines must
    land before that return, or a docker-less box never sees them."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: [])
    monkeypatch.setattr("plexus.tls.status", lambda: ["tls ca: missing", "tls proxy: missing"])
    out = sandbox.doctor()
    assert "tls ca: missing" in out
    assert "tls proxy: missing" in out
    assert "docker: not installed -- no sandboxed run can start" in out


def test_doctor_fix_provisions_tls_only_with_a_chatgpt_seat(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    provisioned = []
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: [])
    monkeypatch.setattr("plexus.tls.provision", lambda: provisioned.append(True))
    monkeypatch.setattr("plexus.tls.status", lambda: [])
    sandbox.doctor(fix=True)
    assert provisioned == [], "no ChatGPT seat -- nothing here uses TLS"


def test_doctor_fix_provisions_tls_when_chatgpt_seat_injected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    provisioned = []
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: ["chatgpt"])
    monkeypatch.setattr("plexus.tls.provision", lambda: provisioned.append(True))
    monkeypatch.setattr("plexus.tls.status", lambda: [])
    sandbox.doctor(fix=True)
    assert provisioned == [True]


def test_doctor_fix_reports_rather_than_crashes_without_openssl(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: ["chatgpt"])

    def fail():
        raise RuntimeError("openssl not installed")

    monkeypatch.setattr("plexus.tls.provision", fail)
    monkeypatch.setattr("plexus.tls.status", lambda: ["tls: MISSING -- openssl not installed"])
    out = sandbox.doctor(fix=True)
    assert any("cannot provision" in line for line in out)
