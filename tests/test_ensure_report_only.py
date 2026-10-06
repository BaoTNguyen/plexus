"""ensure()'s repair is a narrow subset of doctor(fix=True)'s: it restores a
proxy to the state it already ran with, never provisions CA/cert/sentinel
material, and never restarts a running proxy onto newly-computed settings.
docker and openssl are stubbed throughout -- no real container, no network.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_heart_image_check(monkeypatch):
    # image staleness is "reported, never rebuilt" for both doctor and ensure
    # and isn't what this file is about; keep it quiet and off the critical path
    monkeypatch.setattr("heart.sandbox.image_is_stale", lambda _i: None, raising=False)


def _stub_docker(monkeypatch, sandbox, responses):
    calls = []

    def fake(*args, **kw):
        calls.append(args)
        for prefix, result in responses.items():
            if args[:len(prefix)] == prefix:
                return result
        if args[:2] == ("network", "inspect"):
            return (0, "true")  # network exists and is --internal unless overridden
        return (0, "")

    monkeypatch.setattr(sandbox.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(sandbox, "_docker", fake)
    monkeypatch.setattr(sandbox, "reap", lambda: (0, 0))
    return calls


def test_ensure_with_no_ca_does_not_provision_tls(monkeypatch, tmp_path):
    """A ChatGPT seat with no CA yet is exactly the case doctor --fix would
    provision for. ensure() must only report it."""
    from plexus import sandbox

    provisioned = []
    monkeypatch.setattr("plexus.tls.provision", lambda: provisioned.append(True), raising=False)
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: {"chatgpt"})
    monkeypatch.setattr("plexus.registry.seat_secrets", lambda: tmp_path)  # no tls/ under it
    monkeypatch.setattr(sandbox, "lanes", lambda: [(sandbox.NETWORK, sandbox.PROXY, "", "")])
    monkeypatch.setattr(sandbox, "_running_config", lambda _p, running_only=True: {
        "ALLOW": "", "DENY": "", "INJECT_PORT": sandbox.INJECT_PORT, "INJECT_TLS_PORT": "",
        "secrets": True, "codex": True,
    })
    _stub_docker(monkeypatch, sandbox, {})

    out = "\n".join(sandbox.ensure())
    assert not provisioned, "ensure() must never call tls.provision"
    assert "TLS material not yet provisioned" in out
    assert "not restarted" in out


def test_ensure_does_not_restart_a_running_proxy_with_different_settings(monkeypatch):
    """A running proxy whose INJECT_TLS_PORT disagrees with what's wanted is
    reported, not restarted -- a wave in flight needs the proxy it already
    dialed through to keep answering."""
    from plexus import sandbox

    monkeypatch.setattr("plexus.registry.injected_seats", lambda: set())
    monkeypatch.setattr(sandbox, "lanes", lambda: [(sandbox.NETWORK, sandbox.PROXY, "", "")])
    monkeypatch.setattr(sandbox, "_running_config", lambda _p, running_only=True: {
        "ALLOW": "", "DENY": "", "INJECT_PORT": "", "INJECT_TLS_PORT": "9999",
        "secrets": False, "codex": False,
    })
    calls = _stub_docker(monkeypatch, sandbox, {})

    out = "\n".join(sandbox.ensure())
    assert not any(c[0] == "rm" for c in calls), "a running proxy must not be recreated"
    assert not any(c[0] == "run" for c in calls), "a running proxy must not be restarted"
    assert "settings differ from what's wanted" in out
    assert "running:" in out and "wanted:" in out


def test_ensure_restarts_a_stopped_proxy_with_its_running_equivalent_settings(monkeypatch):
    """A stopped proxy is restarted by reusing its own container -- `docker
    start`, never `docker run` with newly-computed settings."""
    from plexus import sandbox

    monkeypatch.setattr("plexus.registry.injected_seats", lambda: set())
    monkeypatch.setattr(sandbox, "lanes", lambda: [(sandbox.NETWORK, sandbox.PROXY, "", "")])
    stopped = {"ALLOW": "", "DENY": "", "INJECT_PORT": "", "INJECT_TLS_PORT": "",
               "secrets": False, "codex": False}
    monkeypatch.setattr(sandbox, "_running_config",
                        lambda _p, running_only=True: None if running_only else stopped)
    calls = _stub_docker(monkeypatch, sandbox, {("start", sandbox.PROXY): (0, "")})

    out = "\n".join(sandbox.ensure())
    assert ("start", sandbox.PROXY) in calls
    assert not any(c[0] == "run" for c in calls), "restarting must not provision a new container"
    assert "restarted with its own last-known-running settings" in out


def test_ensure_stops_when_a_stopped_proxy_cannot_be_restarted(monkeypatch):
    """No running proxy and nothing to restart it with (its secrets are
    missing, or it was never created) is the unworkable case: ensure() must
    stop, not continue as if the sandbox were ready."""
    from plexus import sandbox

    monkeypatch.setattr("plexus.registry.injected_seats", lambda: set())
    monkeypatch.setattr(sandbox, "lanes", lambda: [(sandbox.NETWORK, sandbox.PROXY, "", "")])
    monkeypatch.setattr(sandbox, "_running_config", lambda _p, running_only=True: None)
    _stub_docker(monkeypatch, sandbox, {
        ("start", sandbox.PROXY): (1, "Error: No such container or secrets missing"),
    })

    with pytest.raises(sandbox.SandboxNotReady) as exc:
        sandbox.ensure()
    assert "doctor --fix" in str(exc.value)


def test_ensure_stops_when_docker_is_unavailable(monkeypatch):
    from plexus import sandbox

    monkeypatch.setattr(sandbox.shutil, "which", lambda _n: None)
    with pytest.raises(sandbox.SandboxNotReady):
        sandbox.ensure()


def test_ensure_stops_when_network_repair_fails(monkeypatch):
    from plexus import sandbox

    monkeypatch.setattr("plexus.registry.injected_seats", lambda: set())
    monkeypatch.setattr(sandbox, "lanes", lambda: [(sandbox.NETWORK, sandbox.PROXY, "", "")])
    _stub_docker(monkeypatch, sandbox, {
        ("network", "inspect"): (1, "No such network"),
        ("network", "create"): (1, "create failed"),
    })

    with pytest.raises(sandbox.SandboxNotReady):
        sandbox.ensure()


def test_ensure_attaches_a_running_proxy_after_creating_a_missing_network(monkeypatch):
    from plexus import sandbox

    monkeypatch.setattr("plexus.registry.injected_seats", lambda: set())
    monkeypatch.setattr(sandbox, "lanes", lambda: [(sandbox.NETWORK, sandbox.PROXY, "", "")])
    monkeypatch.setattr(sandbox, "_running_config", lambda _p, running_only=True: {
        "ALLOW": "", "DENY": "", "INJECT_PORT": "", "INJECT_TLS_PORT": "",
        "secrets": False, "codex": False,
    })
    calls = _stub_docker(monkeypatch, sandbox, {
        ("network", "inspect"): (1, "No such network"),
        ("network", "create"): (0, ""),
    })

    out = "\n".join(sandbox.ensure())
    assert ("network", "create", "--internal", sandbox.NETWORK) in calls
    assert ("network", "connect", sandbox.NETWORK, sandbox.PROXY) in calls
    assert "attached to" in out


def test_doctor_fix_still_provisions_tls_and_restarts(monkeypatch, tmp_path):
    """doctor(fix=True) keeps its full unattended power -- only ensure() is
    restricted."""
    from plexus import sandbox

    script = tmp_path / "egress-proxy.py"
    script.write_text("# proxy")
    provisioned = []
    monkeypatch.setattr("plexus.tls.provision", lambda: provisioned.append(True))
    monkeypatch.setattr("plexus.tls.status", lambda: [])
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: {"chatgpt"})
    monkeypatch.setattr(sandbox, "lanes", lambda: [(sandbox.NETWORK, sandbox.PROXY,
                                                     "host.docker.internal:8001", "")])
    monkeypatch.setattr(sandbox, "local_model_hosts", lambda: [])
    monkeypatch.setattr(sandbox, "_running_config", lambda _p, running_only=True: None)
    monkeypatch.setattr(sandbox, "proxy_script", lambda: script)
    monkeypatch.chdir(tmp_path)
    calls = _stub_docker(monkeypatch, sandbox, {})

    sandbox.doctor(fix=True)
    assert provisioned, "doctor(fix=True) still provisions TLS"
    assert any(c[0] == "run" for c in calls), "doctor(fix=True) still restarts the proxy"


def test_ensure_refuses_to_start_a_stopped_proxy_whose_secrets_are_gone(monkeypatch, tmp_path):
    """The secrets mount is a directory, so `docker start` succeeds over an
    emptied one and the injector then refuses everything. ensure() must check
    the files the stopped container was created to read, and not start it."""
    from plexus import sandbox

    monkeypatch.setattr("plexus.registry.injected_seats", lambda: {"anthropic"})
    monkeypatch.setattr("plexus.registry.seat_secrets", lambda: tmp_path)  # no sentinel
    monkeypatch.setattr(sandbox, "lanes", lambda: [(sandbox.NETWORK, sandbox.PROXY, "", "")])
    stopped = {"ALLOW": "", "DENY": "", "INJECT_PORT": sandbox.INJECT_PORT,
               "INJECT_TLS_PORT": "", "secrets": True, "codex": False}
    monkeypatch.setattr(sandbox, "_running_config",
                        lambda _p, running_only=True: None if running_only else stopped)
    calls = _stub_docker(monkeypatch, sandbox, {})

    with pytest.raises(sandbox.SandboxNotReady) as exc:
        sandbox.ensure()
    assert "sentinel" in str(exc.value) and "doctor --fix" in str(exc.value)
    assert ("start", sandbox.PROXY) not in calls
    assert not (tmp_path / "sentinel").exists(), "ensure() must not create the sentinel"
