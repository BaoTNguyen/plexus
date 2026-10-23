"""trust_boundary: `[review] boundary` entries a diff touches (F88).

heart's sandbox-trace-env changed what env reaches a container inside
episode._sandbox_profiles and landed unheld, because nothing imports that
private function from another repo. These diffs come from real `git diff`
in a scratch repo, so the hunk headers are git's own.
"""
import subprocess
import textwrap

from plexus import review
from plexus.spec import load_spec

MODULE = textwrap.dedent('''\
    import os


    def _sandbox_profiles(task):
        env = {k: v for k, v in os.environ.items() if k.startswith("ARTERIES_")}
        a = 1
        b = 2
        c = 3
        d = 4
        return env


    def _unrelated(x):
        y = x + 1
        z = y + 1
        w = z + 1
        v = w + 1
        return v
''')


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _diff(tmp_path, old, new):
    assert old != new, "the edit did not apply"
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    (repo / "src" / "episode.py").write_text(old)
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    (repo / "src" / "episode.py").write_text(new)
    return _git(repo, "diff")


BOUNDARY = ("src/sandbox.py", "src/episode.py::_sandbox_profiles")


def test_change_inside_the_named_function_is_a_hit(tmp_path):
    new = MODULE.replace("    d = 4\n",
                         "    d = 4\n    env['PULSE_TRACE_ID'] = 'x'\n")
    diff = _diff(tmp_path, MODULE, new)
    assert review.trust_boundary(diff, BOUNDARY) == ["src/episode.py::_sandbox_profiles"]


def test_change_in_another_function_of_the_same_file_is_not(tmp_path):
    new = MODULE.replace("    v = w + 1\n", "    v = w + 2\n")
    diff = _diff(tmp_path, MODULE, new)
    assert review.trust_boundary(diff, BOUNDARY) == []


def test_renaming_the_def_is_a_hit(tmp_path):
    new = MODULE.replace("def _sandbox_profiles(task):", "def _sandbox_profiles(task, extra=None):")
    diff = _diff(tmp_path, MODULE, new)
    assert review.trust_boundary(diff, BOUNDARY) == ["src/episode.py::_sandbox_profiles"]


def test_path_entry_matches_any_change_and_empty_config_matches_nothing():
    diff = ("diff --git a/src/sandbox.py b/src/sandbox.py\n--- a/src/sandbox.py\n"
            "+++ b/src/sandbox.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n")
    assert review.trust_boundary(diff, BOUNDARY) == ["src/sandbox.py"]
    assert review.trust_boundary(diff, ()) == []


def test_plexus_toml_boundary_is_read(tmp_path):
    (tmp_path / "plexus.toml").write_text(
        '[goal]\nid = "g"\ntext = "t"\n[ground_truth]\nsuite = "true"\n[review]\nboundary = ["src/sandbox.py", "src/e.py::f"]\n')
    assert load_spec(tmp_path).review_boundary == ("src/sandbox.py", "src/e.py::f")


def test_change_to_the_first_body_line_is_a_hit(tmp_path):
    # PLEXUS-B35: git's header names `import os`; the def is a context line
    new = MODULE.replace('if k.startswith("ARTERIES_")}', 'if k.startswith(("ARTERIES_", "PULSE_"))}')
    diff = _diff(tmp_path, MODULE, new)
    assert review.trust_boundary(diff, BOUNDARY) == ["src/episode.py::_sandbox_profiles"]


def test_a_pure_rename_of_a_boundary_file_is_a_hit():
    diff = ("diff --git a/src/sandbox.py b/src/box.py\nsimilarity index 100%\n"
            "rename from src/sandbox.py\nrename to src/box.py\n")
    assert review.trust_boundary(diff, BOUNDARY) == ["src/sandbox.py"]
