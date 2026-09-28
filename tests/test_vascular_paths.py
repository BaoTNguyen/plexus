"""Tests for src/plexus/vascular_paths.py -- stdlib only, no frameworks."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from plexus import registry, sandbox, vascular_paths


def test_home_respects_VASCULAR_HOME(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VASCULAR_HOME", d)
        assert vascular_paths.home() == Path(d)


def test_home_defaults_to_dot_vascular_in_home(monkeypatch):
    monkeypatch.delenv("VASCULAR_HOME", raising=False)
    h = vascular_paths.home()
    assert h == Path.home() / ".vascular"


def test_KINDS_is_a_tuple_of_five(monkeypatch):
    assert vascular_paths.KINDS == ("config", "state", "cache", "data", "backups")
    assert len(vascular_paths.KINDS) == 5


def test_path_returns_correct_structure(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VASCULAR_HOME", d)
        p = vascular_paths.path("config", "myapp", "settings", "local.toml")
        assert p == Path(d) / "config" / "myapp" / "settings" / "local.toml"


def test_path_refuses_unknown_kind():
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


def test_journal_dir_defaults_to_heart_state(monkeypatch, tmp_path):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.delenv("EVENT_JOURNAL_DIR", raising=False)
        monkeypatch.setenv("VASCULAR_HOME", d)
        expected = Path(d) / "state" / "heart" / "events"
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
