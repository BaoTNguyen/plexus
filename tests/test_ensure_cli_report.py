"""`plexus run` under HEART_SANDBOX: every skipped credential/TLS/proxy-setting
change from ensure() prints as a `sandbox: ` line telling the operator to run
`plexus doctor --fix` after review, and the command only exits non-zero when
ensure() finds no workable state at all. docker is stubbed throughout --
real credential/TLS/proxy-setting inputs are faked, never a container.
"""
import pytest

from plexus import cli, registry, run as run_mod, sandbox, spec


@pytest.fixture(autouse=True)
def _isolate_and_stub(monkeypatch, tmp_path):
    # the CLI always calls registry.seat_env() first; keep it off real HOME
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path / "vascular"))
    monkeypatch.setenv("PLEXUS_DEV_ENV", "off")
    monkeypatch.setenv("HEART_SANDBOX", "1")
    monkeypatch.setattr("heart.sandbox.image_is_stale", lambda _i: None, raising=False)
    monkeypatch.setattr("plexus.sandbox._baked_heart_drift", lambda: None)
    monkeypatch.setattr(sandbox, "local_model_hosts", lambda: [])
    monkeypatch.setattr(sandbox, "reap", lambda: (0, 0))
    # the real run loop is not what this file tests; a run invoked past
    # ensure() must still return cleanly
    monkeypatch.setattr(run_mod, "run", lambda *a, **kw: 0)
    monkeypatch.setattr(spec, "load_spec", lambda _root: object())


def _stub_docker(monkeypatch, responses):
    calls = []

    def fake(*args, **kw):
        calls.append(args)
        for prefix, result in responses.items():
            if args[:len(prefix)] == prefix:
                return result
        if args[:2] == ("network", "inspect"):
            return (0, "true")
        return (0, "")

    monkeypatch.setattr(sandbox.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(sandbox, "_docker", fake)
    return calls


def _sandbox_lines(capsys):
    out = capsys.readouterr().out
    return [l for l in out.splitlines() if l.startswith("sandbox: ")]


def test_run_reports_a_skipped_credential_change(monkeypatch, tmp_path, capsys):
    """A mounted (non-injected) claude seat is a credential plexus will not
    touch -- it must be reported with the doctor --fix follow-up, not silently
    carried."""
    monkeypatch.setattr(registry, "injected_seats", lambda: set())
    monkeypatch.setattr(registry, "detect_subscriptions", lambda: {"claude": 100.0})
    monkeypatch.setattr(sandbox, "lanes", lambda: [(sandbox.NETWORK, sandbox.PROXY, "", "")])
    monkeypatch.setattr(sandbox, "_running_config", lambda _p, running_only=True: {
        "ALLOW": "", "DENY": "", "INJECT_PORT": "", "INJECT_TLS_PORT": "",
        "secrets": "", "codex": False, "log": True,
    })
    _stub_docker(monkeypatch, {})

    rc = cli.main(["run", "--root", str(tmp_path)])

    assert rc == 0
    lines = _sandbox_lines(capsys)
    assert any("seat claude: MOUNTED" in l for l in lines)
    assert any("plexus doctor --fix` to restart the proxies with it" in l for l in lines)


def test_run_reports_a_skipped_proxy_setting_change(monkeypatch, tmp_path, capsys):
    """A running proxy whose settings disagree with what's wanted must be
    reported, not restarted -- ensure() never touches a running proxy."""
    monkeypatch.setattr(registry, "injected_seats", lambda: set())
    monkeypatch.setattr(registry, "detect_subscriptions", lambda: {})
    monkeypatch.setattr(sandbox, "lanes",
                        lambda: [(sandbox.NETWORK, sandbox.PROXY, "wanted.example:443", "")])
    monkeypatch.setattr(sandbox, "_running_config", lambda _p, running_only=True: {
        "ALLOW": "stale.example:443", "DENY": "", "INJECT_PORT": "", "INJECT_TLS_PORT": "",
        "secrets": "", "codex": False,
    })
    calls = _stub_docker(monkeypatch, {})

    rc = cli.main(["run", "--root", str(tmp_path)])

    assert rc == 0
    assert not any(c[0] == "run" for c in calls), "a running proxy must not be recreated"
    lines = _sandbox_lines(capsys)
    assert any("settings differ from what's wanted" in l for l in lines)
    assert any("not restarted" in l and "plexus doctor --fix` after reviewing" in l for l in lines)


def test_run_reports_a_skipped_tls_change(monkeypatch, tmp_path, capsys):
    """A ChatGPT seat injected with no CA/cert material yet is exactly what
    doctor --fix would provision -- ensure() must only report it."""
    import time

    monkeypatch.setattr(registry, "injected_seats", lambda: {"chatgpt"})
    monkeypatch.setattr(registry, "detect_subscriptions", lambda: {})
    monkeypatch.setattr(registry, "codex_claims", lambda: {"exp": time.time() + 999999})
    monkeypatch.setattr(registry, "seat_secrets", lambda: tmp_path / "secrets")  # no tls/ under it
    monkeypatch.setattr(sandbox, "lanes", lambda: [(sandbox.NETWORK, sandbox.PROXY, "", "")])
    monkeypatch.setattr(sandbox, "_running_config", lambda _p, running_only=True: {
        "ALLOW": "", "DENY": "", "INJECT_PORT": sandbox.INJECT_PORT, "INJECT_TLS_PORT": "",
        "secrets": str(tmp_path / "secrets"), "codex": True, "log": True,
    })
    calls = _stub_docker(monkeypatch, {})

    rc = cli.main(["run", "--root", str(tmp_path)])

    assert rc == 0
    assert not any(c[0] == "run" for c in calls), "TLS material must never be provisioned here"
    lines = _sandbox_lines(capsys)
    assert any("TLS material not yet provisioned" in l for l in lines)
    assert any("not restarted" in l and "plexus doctor --fix` after reviewing" in l for l in lines)


def test_run_stops_nonzero_when_sandbox_is_not_workable(monkeypatch, tmp_path, capsys):
    """No docker at all is the unworkable case: the run must not proceed as
    if the sandbox were ready."""
    monkeypatch.setattr(sandbox.shutil, "which", lambda _n: None)

    rc = cli.main(["run", "--root", str(tmp_path)])

    assert rc == 1
    lines = _sandbox_lines(capsys)
    assert any("doctor --fix" in l for l in lines)


def test_run_skips_ensure_when_sandbox_is_off(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HEART_SANDBOX", "off")

    def _unexpected(_n):
        raise AssertionError("ensure() must not run when HEART_SANDBOX=off")

    monkeypatch.setattr(sandbox.shutil, "which", _unexpected)

    rc = cli.main(["run", "--root", str(tmp_path)])

    assert rc == 0
    assert _sandbox_lines(capsys) == []


def test_run_refuses_an_image_whose_baked_heart_drifted(monkeypatch, tmp_path, capsys):
    """F38: verifiers import the image's heart, so a stale one judges every
    episode by old code. That is certain to mislead, so it stops the run."""
    monkeypatch.setattr(registry, "injected_seats", lambda: set())
    monkeypatch.setattr(registry, "detect_subscriptions", lambda: {})
    monkeypatch.setattr(sandbox, "lanes", lambda: [])
    monkeypatch.setattr(sandbox, "_baked_heart_drift",
                        lambda: "sandbox image 'x' bakes heart abc, ...; rebuild it")
    _stub_docker(monkeypatch, {})

    rc = cli.main(["run", "--root", str(tmp_path)])

    assert rc == 1
    assert any("bakes heart abc" in l for l in _sandbox_lines(capsys))


@pytest.mark.parametrize("cmd", ["plan", "approve", "run"])
def test_an_unset_sandbox_refuses_instead_of_running_on_the_host(
        monkeypatch, tmp_path, capsys, cmd):
    """F71/F79: unset used to mean "on this host" without a word."""
    monkeypatch.delenv("HEART_SANDBOX")

    assert cli.main([cmd, "--root", str(tmp_path)]) == 1
    assert "HEART_SANDBOX is unset" in capsys.readouterr().err


def test_sandbox_off_on_purpose_still_runs_but_says_so(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HEART_SANDBOX", "off")

    assert cli.main(["run", "--root", str(tmp_path)]) == 0
    assert "WARNING: HEART_SANDBOX=off" in capsys.readouterr().err
