"""The four gaps reviewers found in cross_repo_callers (667e9b8): sibling paths
with spaces, names exported from __init__.py, renamed modules, and the run.py
half that raises a caller-bearing feature to boundary."""
import subprocess

from plexus import ledger
from plexus import review
from plexus import run as run_mod
from test_cross_repo_boundary import API_SRC, _git, _git_out, _make_lib
from test_unreviewed_land import (_add_diff, _episode, _make_repo, _spec, _write_plan,
                                  stub_heart)  # noqa: F401


def _make_caller(tmp_path, name: str, source: str):
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / name).write_text(source)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")


def _working_diff(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), "diff", *args],
                          capture_output=True, text=True, check=True).stdout


def test_sibling_path_with_spaces_is_scanned(tmp_path):
    lib = _make_lib(tmp_path)
    _make_caller(tmp_path, "my app.py", "from lib.api import f\n")
    base = _git_out(lib, "rev-parse", "HEAD")
    api = lib / "src" / "lib" / "api.py"
    api.write_text(API_SRC.replace("return 1", "return 100"))
    diff = _working_diff(lib)
    assert review.cross_repo_callers(lib, base, diff) == ["app/my app.py:1"]


def test_init_names_are_qualified_as_the_package(tmp_path):
    lib = _make_lib(tmp_path)
    init = lib / "src" / "lib" / "__init__.py"
    init.write_text("def top():\n    return 1\n")
    _git(lib, "commit", "-qam", "export top")
    _make_caller(tmp_path, "app.py", "from lib import top\n")
    base = _git_out(lib, "rev-parse", "HEAD")
    init.write_text("def top():\n    return 2\n")
    assert review.cross_repo_callers(lib, base, _working_diff(lib)) == ["app/app.py:1"]


def test_renamed_module_drops_every_public_name(tmp_path):
    lib = _make_lib(tmp_path)
    _make_caller(tmp_path, "app.py", "from lib.api import g\n")
    base = _git_out(lib, "rev-parse", "HEAD")
    _git(lib, "mv", "src/lib/api.py", "src/lib/core.py")
    diff = _working_diff(lib, "--cached", "-M")
    assert "rename from" in diff
    assert review.cross_repo_callers(lib, base, diff) == ["app/app.py:1"]


def test_callers_raise_the_land_to_a_boundary_hold(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(run_mod, "cross_repo_callers", lambda *a: ["app/app.py:1"])
    ep = _episode(repo, "runs", "ep-1", _add_diff(repo), review_verdict=None)
    _write_plan(repo, {"id": "f1", "title": "t", "spec": "s", "acceptance": "true",
                       "touches": None})
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id="")
    monkeypatch.setattr(run_mod, "_build", lambda *a, **k: [ep])
    spec = _spec(pipeline=False, review_hold=("boundary",))
    assert run_mod._walk(spec, repo, "runs", 1, "") == 1
    recs = ledger.read(repo)
    esc = [r for r in recs if r["kind"] == "escalation.raised"]
    assert esc and esc[0]["reason_class"] == "held_for_review"
    assert esc[0]["reason"].startswith("boundary-class")
    assert "(called from: app/app.py:1)" in esc[0]["reason"]
    assert not any(r["kind"] == "feature.landed" for r in recs)
