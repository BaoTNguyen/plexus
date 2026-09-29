"""cross_repo_callers: does a diff touch a name a sibling checkout still uses.

Builds two sibling git repos under tmp_path -- `lib` (the one being diffed)
and `app` (an unrelated checkout that imports from it) -- and drives
plexus.review.cross_repo_callers directly against real diffs.
"""
import subprocess
from pathlib import Path

from plexus import review

API_SRC = """def f():
    return 1


def g():
    return 2


def _p():
    return 3
"""


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _git_out(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_lib(tmp_path) -> Path:
    repo = tmp_path / "lib"
    (repo / "src" / "lib").mkdir(parents=True)
    (repo / "src" / "lib" / "__init__.py").write_text("")
    (repo / "src" / "lib" / "api.py").write_text(API_SRC)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _make_app(tmp_path) -> Path:
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "app.py").write_text("from lib.api import f\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _diff_edit(repo: Path, new_content: str) -> str:
    """A real, applicable patch changing src/lib/api.py, tree left as it was."""
    target = repo / "src" / "lib" / "api.py"
    original = target.read_text()
    target.write_text(new_content)
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--", "src/lib/api.py"],
                          capture_output=True, text=True, check=True).stdout
    target.write_text(original)
    return diff


def _diff_delete(repo: Path) -> str:
    target = repo / "src" / "lib" / "api.py"
    original = target.read_text()
    target.unlink()
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--", "src/lib/api.py"],
                          capture_output=True, text=True, check=True).stdout
    target.write_text(original)
    return diff


def test_changed_public_function_has_a_caller(tmp_path):
    lib, app = _make_lib(tmp_path), _make_app(tmp_path)
    base = _git_out(lib, "rev-parse", "HEAD")
    diff = _diff_edit(lib, API_SRC.replace("return 1", "return 100"))
    assert review.cross_repo_callers(lib, base, diff) == ["app/app.py:1"]


def test_changed_function_nobody_imports_has_no_caller(tmp_path):
    lib, app = _make_lib(tmp_path), _make_app(tmp_path)
    base = _git_out(lib, "rev-parse", "HEAD")
    diff = _diff_edit(lib, API_SRC.replace("return 2", "return 200"))
    assert review.cross_repo_callers(lib, base, diff) == []


def test_changed_private_helper_has_no_caller(tmp_path):
    lib, app = _make_lib(tmp_path), _make_app(tmp_path)
    base = _git_out(lib, "rev-parse", "HEAD")
    diff = _diff_edit(lib, API_SRC.replace("return 3", "return 300"))
    assert review.cross_repo_callers(lib, base, diff) == []


def test_new_public_name_has_no_caller(tmp_path):
    lib, app = _make_lib(tmp_path), _make_app(tmp_path)
    base = _git_out(lib, "rev-parse", "HEAD")
    diff = _diff_edit(lib, API_SRC + "\n\ndef h():\n    return 4\n")
    assert review.cross_repo_callers(lib, base, diff) == []


def test_deleted_function_still_has_a_caller(tmp_path):
    lib, app = _make_lib(tmp_path), _make_app(tmp_path)
    base = _git_out(lib, "rev-parse", "HEAD")
    diff = _diff_delete(lib)
    assert review.cross_repo_callers(lib, base, diff) == ["app/app.py:1"]
