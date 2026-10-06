"""The egress proxies log under the `log` kind: a read-write /log mount and a
LOG_FILE named for the container, and a running proxy without it is stale."""
import pytest

from plexus import sandbox


@pytest.fixture
def vhome(monkeypatch, tmp_path):
    home = tmp_path / "vascular"
    monkeypatch.setenv("VASCULAR_HOME", str(home))
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: set())
    monkeypatch.setattr("plexus.registry.seat_secrets", lambda: tmp_path / "secrets")
    return home


@pytest.mark.parametrize("proxy", ["egress", "egress-web"])
def test_start_proxy_mounts_log_dir_and_sets_log_file(monkeypatch, tmp_path, vhome, proxy):
    calls = []
    monkeypatch.setattr(sandbox, "_docker", lambda *a, **k: calls.append(a) or (0, ""))
    want = sandbox._wanted_config("")
    sandbox._start_proxy(proxy, sandbox.NETWORK, want, tmp_path / "proxy.py")
    run = next(c for c in calls if c[0] == "run")
    log_dir = vhome / "log" / "heart"
    i = run.index(f"{log_dir}:/log")
    assert run[i - 1] == "-v"
    i = run.index(f"LOG_FILE=/log/{proxy}.log")
    assert run[i - 1] == "-e"
    assert log_dir.is_dir()


def test_running_proxy_without_log_mount_differs_from_wanted(monkeypatch, vhome):
    monkeypatch.setattr(sandbox, "_docker", lambda *a, **k: (
        0, "true\t/proxy.py \tALLOW=\nDENY=\n"))
    have = sandbox._running_config("egress")
    want = sandbox._wanted_config("")
    assert have["log"] is False and want["log"] is True
    assert {k: v for k, v in have.items() if k != "log"} == \
           {k: v for k, v in want.items() if k != "log"}


def _stub_agreeing(monkeypatch, tmp_path, mounts):
    """An injected seat, and a canned inspect line whose ALLOW/DENY/ports
    already agree with what's wanted -- only the mounts are under test."""
    monkeypatch.setattr("plexus.registry.injected_seats", lambda: {"anthropic"})
    monkeypatch.setattr(sandbox, "_docker", lambda *a, **k: (
        0, f"true\t{tmp_path}/proxy.py=/proxy.py {mounts}\t"
           f"ALLOW=a.example:443\nDENY=b.example:443\nINJECT_PORT={sandbox.INJECT_PORT}\n"))


def test_running_proxy_with_moved_secrets_differs_from_wanted(monkeypatch, tmp_path, vhome):
    """F72: /secrets bound to an old secrets dir is stale, even though a
    /secrets mount exists -- doctor --fix must see it as differing."""
    moved = tmp_path / "old-secrets"
    _stub_agreeing(monkeypatch, tmp_path, f"{moved}=/secrets {vhome}/log/heart=/log")
    have = sandbox._running_config("egress")
    want = sandbox._wanted_config("a.example:443", "b.example:443")
    assert have["secrets"] == str(moved)
    assert want["secrets"] == str(tmp_path / "secrets")
    assert have != want


def test_running_proxy_with_matching_secrets_source_and_log_equals_wanted(
        monkeypatch, tmp_path, vhome):
    _stub_agreeing(monkeypatch, tmp_path,
                   f"{tmp_path / 'secrets'}=/secrets {vhome}/log/heart=/log")
    have = sandbox._running_config("egress")
    want = sandbox._wanted_config("a.example:443", "b.example:443")
    assert have == want


def test_running_proxy_without_secrets_mount_reports_empty_source(monkeypatch, tmp_path, vhome):
    _stub_agreeing(monkeypatch, tmp_path, f"{vhome}/log/heart=/log")
    assert sandbox._running_config("egress")["secrets"] == ""
