"""Tests for src/plexus/vascular_paths.py -- stdlib only, no frameworks."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from plexus import registry, sandbox, vascular_paths


def test_home_respects_VASCULAR_HOME(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VASCULAR_HOME", d)
        assert vascular_paths.home() == Path(d)


def test_home_defaults_to_dot_vascular_in_home(monkeypatch, tmp_path):
    monkeypatch.delenv("VASCULAR_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    h = vascular_paths.home()
    assert h == Path.home() / ".vascular"


def test_KINDS_is_the_eight_kinds_in_order(monkeypatch, tmp_path):
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path))
    assert vascular_paths.KINDS == (
        "config", "secrets", "state", "spool", "log", "cache", "data", "backups")
    assert len(vascular_paths.KINDS) == 8


def test_new_kinds_resolve_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path))
    for kind in ("secrets", "spool", "log"):
        p = vascular_paths.path(kind, "x")
        assert p == vascular_paths.home() / kind / "x"
        assert p == tmp_path / kind / "x"


def test_path_returns_correct_structure(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VASCULAR_HOME", d)
        p = vascular_paths.path("config", "myapp", "settings", "local.toml")
        assert p == Path(d) / "config" / "myapp" / "settings" / "local.toml"


def test_path_refuses_unknown_kind(monkeypatch, tmp_path):
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="unknown kind 'foobar'"):
        vascular_paths.path("foobar", "myapp")


def test_path_creates_nothing(monkeypatch, tmp_path):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VASCULAR_HOME", d)
        p = vascular_paths.path("state", "test", "a", "b", "c")
        assert not p.exists(), "path() must never create directories"


def test_journal_dir_uses_ENV(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("EVENT_JOURNAL_DIR", d)
        assert vascular_paths.journal_dir() == Path(d)


def test_journal_dir_defaults_to_events_spool(monkeypatch, tmp_path):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.delenv("EVENT_JOURNAL_DIR", raising=False)
        monkeypatch.setenv("VASCULAR_HOME", d)
        expected = Path(d) / "spool" / "events"
        assert vascular_paths.journal_dir() == expected


def test_repo_dir_constructs_path(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VASCULAR_HOME", d)
        p = vascular_paths.repo_dir("/my/repo", "capillaries")
        assert p == Path("/my/repo") / ".vascular" / "capillaries"


def test_repo_dir_accepts_Path(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VASCULAR_HOME", d)
        p = vascular_paths.repo_dir(Path("/another/repo"), "heart")
        assert p == Path("/another/repo") / ".vascular" / "heart"


def test_registry_paths_follow_vascular_home(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VASCULAR_HOME", d)
        monkeypatch.delenv("PLEXUS_REGISTRY", raising=False)
        monkeypatch.delenv("PLEXUS_WORKSPACE", raising=False)
        expected = Path(d) / "config" / "plexus"
        assert registry._registry_path() == expected / "registry.json"
        assert registry._workspace_path() == expected / "workspace.json"


def test_sandbox_reads_models_json_from_vascular_home(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VASCULAR_HOME", d)
        models = Path(d) / "config" / "heart" / "models.json"
        models.parent.mkdir(parents=True)
        models.write_text(json.dumps(
            {"profiles": {"local": {"endpoint": "http://127.0.0.1:8001/v1"}}}))
        assert sandbox.local_model_hosts() == ["host.docker.internal:8001"]


def test_plexus_registry_overrides_vascular_home(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VASCULAR_HOME", d)
        elsewhere = Path(d) / "elsewhere.json"
        monkeypatch.setenv("PLEXUS_REGISTRY", str(elsewhere))
        assert registry._registry_path() == elsewhere


def test_seat_secrets_under_vascular_home(monkeypatch, tmp_path):
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path / "vascular"))
    expected = tmp_path / "vascular" / "secrets" / "heart"
    assert registry.seat_secrets() == expected
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert registry.seat_secrets() == expected


def test_sentinel_seed_locks_down_both_secret_dirs(monkeypatch, tmp_path):
    home = tmp_path / "vascular"
    monkeypatch.setenv("VASCULAR_HOME", str(home))
    old = os.umask(0o022)
    try:
        seed = registry.sentinel_seed()
    finally:
        os.umask(old)
    assert seed == home / "secrets" / "heart" / "sentinel"
    assert stat.S_IMODE((home / "secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "secrets" / "heart").stat().st_mode) == 0o700
    assert stat.S_IMODE(seed.stat().st_mode) == 0o600
