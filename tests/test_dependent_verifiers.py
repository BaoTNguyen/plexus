"""F85: a change that breaks a repo importing this one must fail its episode.

Real repos and a real heart verifier run (sandbox off): `lib` is the repo
under test, `app` imports it and pins today's behaviour. The dependent
verifier must judge the *candidate* lib, not whichever lib is installed.
"""
import subprocess
from pathlib import Path

import pytest

from heart.env import Workspace
from heart.verify import run_verifiers
from plexus import run
from plexus.spec import GoalSpec


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "init", "-q")
    _git(path, "-c", "user.email=t@e", "-c", "user.name=T", "add", ".")
    _git(path, "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-qm", "base")
    return path


def _spec(**kw) -> GoalSpec:
    fields = dict(goal_id="g", text="t", context="", suite="true",
                  attempts_per_feature=1, episodes_per_goal=5, agent="claude",
                  agent_cmd=None, timeout=60, spec_hash="h")
    return GoalSpec(**(fields | kw))


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("HEART_SANDBOX", "off")
    lib = _repo(tmp_path / "lib", {"src/lib/__init__.py": "VALUE = 1\n"})
    app = _repo(tmp_path / "app", {
        "plexus.toml": '[ground_truth]\nsuite = "python3 -m pytest -q -p no:cacheprovider"\n',
        "tests/test_app.py": "import lib\n\ndef test_value():\n    assert lib.VALUE == 1\n"})
    monkeypatch.setattr(run, "load_registry", lambda: {"app": str(app)})
    return lib


def _judge(lib: Path, new_value: str) -> dict:
    verifiers, mounts = run._dependent_verifiers(_spec(dependents=("app",)), str(lib))
    assert [v.name for v in verifiers] == ["dependent:app"] and mounts == {}
    ws = Workspace(str(lib), subprocess.run(
        ["git", "-C", str(lib), "rev-parse", "HEAD"], capture_output=True,
        text=True, check=True).stdout.strip())
    try:
        (Path(ws.path) / "src/lib/__init__.py").write_text(f"VALUE = {new_value}\n")
        return run_verifiers(verifiers, str(ws.path), 60)["dependent:app"]
    finally:
        ws.destroy()


def test_a_candidate_that_breaks_a_dependent_fails_its_verifier(stack):
    r = _judge(stack, "2")
    assert not r["passed"], r["output_tail"]
    assert "assert 2 == 1" in r["output_tail"]


def test_a_candidate_that_keeps_a_dependent_working_passes(stack):
    assert _judge(stack, "1")["passed"]


def test_an_unknown_dependent_stops_the_run(stack):
    with pytest.raises(SystemExit, match="'nope'"):
        run._dependent_verifiers(_spec(dependents=("nope",)), str(stack))


def test_under_the_sandbox_the_dependent_is_mounted_not_reached(stack, monkeypatch):
    monkeypatch.setenv("HEART_SANDBOX", run.SANDBOX_MODE)
    verifiers, mounts = run._dependent_verifiers(_spec(dependents=("app",)), str(stack))
    assert list(mounts.values()) == ["/deps/app"]
    assert "cd /deps/app" in verifiers[0].command
