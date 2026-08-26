"""API-pin test: plexus imports heart as a library from a sibling source
checkout (see heart's STACK_READINESS.md 1.2), so a heart refactor can break
plexus silently — there is no packaging boundary to catch it. This test fails
loudly, in plexus's own suite, the moment any heart symbol plexus depends on
is renamed, removed, or reordered.

Pinned surface (from src/plexus/run.py and src/plexus/plan.py):
    from heart.detect import detect_verifiers
    from heart.env import Workspace
    from heart.episode import best_episode, run_candidates
    from heart.runner import CACHE_MULTIPLIERS
    from heart.taskspec import TaskSpec

Two layers of pin:
  1. `inspect.signature` on each callable/class checks that every parameter
     name plexus actually passes by keyword still exists. A heart change that
     only *adds* an optional parameter is fine and won't fail this; a rename
     or removal of a parameter plexus uses will.
  2. One real end-to-end run reproducing run.py's call pattern: build a toy
     git repo (same pattern as heart/tests/test_heart.py's make_repo),
     construct a TaskSpec the way run.py does, run detect_verifiers on it,
     drive Workspace the way _run_acceptance does, then run_candidates +
     best_episode the way the feature loop does, and assert a real "pass".
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from heart.detect import detect_verifiers
from heart.env import Workspace
from heart.episode import best_episode, run_candidates
from heart.runner import CACHE_MULTIPLIERS
from heart.taskspec import TaskSpec

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = (
    "import unittest\nfrom calc import add\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
    "if __name__ == '__main__':\n    unittest.main()\n"
)
FIX_CMD = "sed -i 's/a - b/a + b/' calc.py"


def make_repo(root: Path) -> str:
    """Same pattern as heart/tests/test_heart.py's make_repo: a calc.py with
    a bug, a unittest file, one commit."""
    repo = root / "toyrepo"
    repo.mkdir()
    (repo / "calc.py").write_text(BUGGY)
    (repo / "test_calc.py").write_text(TEST)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "buggy add"], check=True)
    return subprocess.run(
        [*git[:3], "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


class TestHeartApiPin(unittest.TestCase):
    """Pins the exact heart API surface plexus depends on."""

    # ---- signature pins -------------------------------------------------
    # Each assertion mirrors an actual call site in src/plexus/run.py (or
    # plan.py). Parameter *names* matter because plexus calls these mostly
    # by keyword; a rename breaks plexus even if positional order is kept.

    def test_taskspec_fields_plexus_constructs(self):
        # run.py: TaskSpec(task_id=..., repo_path=..., base_commit=...,
        #                   prompt=..., public_verifiers=..., timeout_seconds=...)
        params = inspect.signature(TaskSpec.__init__).parameters
        for name in ("task_id", "repo_path", "base_commit", "prompt",
                     "public_verifiers", "timeout_seconds"):
            self.assertIn(name, params, f"TaskSpec lost/renamed field {name!r}")

    def test_detect_verifiers_signature(self):
        # run.py: detect_verifiers(repo)  -- single positional repo_path
        params = list(inspect.signature(detect_verifiers).parameters)
        self.assertGreaterEqual(len(params), 1)
        # first (and plexus's only) argument must still accept a bare repo path
        first = inspect.signature(detect_verifiers).parameters[params[0]]
        self.assertIn(
            first.kind,
            (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD),
        )

    def test_workspace_signature(self):
        # run.py's _run_acceptance: Workspace(str(repo), base_commit)
        # plan.py also constructs/uses Workspace the same positional way.
        params = inspect.signature(Workspace.__init__).parameters
        names = list(params)
        # first two params after self must accept (repo_path, commit) positionally
        self.assertGreaterEqual(len(names), 2)
        for pname in names[:2]:
            self.assertIn(
                params[pname].kind,
                (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD),
            )
        # methods plexus calls on the instance: ws.apply(diff), ws.destroy()
        self.assertIn("patch", inspect.signature(Workspace.apply).parameters)
        inspect.signature(Workspace.destroy)  # must still exist/be callable

    def test_run_candidates_signature(self):
        # run.py: run_candidates(task, candidates, agent=spec.agent,
        #                         agent_cmd=spec.agent_cmd, runs_dir=str(...))
        sig = inspect.signature(run_candidates)
        params = sig.parameters
        names = list(params)
        self.assertGreaterEqual(len(names), 2)  # task, n positional
        # agent/agent_cmd/runs_dir are passed by keyword straight through to
        # run_episode via **kwargs, so run_candidates must still accept
        # arbitrary keywords (a bare **kwargs, or the names explicitly)
        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        explicit = {"agent", "agent_cmd", "runs_dir"} <= set(names)
        self.assertTrue(
            has_var_kw or explicit,
            "run_candidates must accept agent=/agent_cmd=/runs_dir= "
            "(directly or via **kwargs) — plexus's run.py passes them by keyword",
        )

    def test_best_episode_signature(self):
        # run.py: best_episode(run_candidates(...)) -- single positional list
        params = list(inspect.signature(best_episode).parameters)
        self.assertGreaterEqual(len(params), 1)

    def test_pipeline_symbols(self):
        # run.py: from heart.episode import DEFAULT_ROLES, best_episode, run_candidates
        # and passes roles=DEFAULT_ROLES into run_candidates.
        from heart.episode import DEFAULT_ROLES, run_episode
        self.assertTrue(DEFAULT_ROLES and
                        all("name" in r and "prompt" in r for r in DEFAULT_ROLES),
                        "DEFAULT_ROLES lost its {name,prompt} shape plexus relies on")
        # roles= flows through **kwargs to run_episode, which must still accept it
        self.assertIn("roles", inspect.signature(run_episode).parameters)

    def test_cache_multipliers_shape(self):
        """serve.py prices subscription turns off its own per-provider rate card
        and multiplies cache buckets by these. Heart owns the constant so the two
        repos cannot drift; if heart renames a bucket, plexus's dollars go quietly
        wrong rather than loudly missing, which is why this is pinned by key."""
        self.assertEqual(set(CACHE_MULTIPLIERS),
                         {"cache_read", "cache_write_5m", "cache_write_1h"})
        # a read must stay cheaper than fresh input and a write dearer, or the
        # arithmetic downstream is inverted rather than merely off
        self.assertLess(CACHE_MULTIPLIERS["cache_read"], 1.0)
        self.assertGreater(CACHE_MULTIPLIERS["cache_write_5m"], 1.0)
        self.assertGreater(CACHE_MULTIPLIERS["cache_write_1h"],
                           CACHE_MULTIPLIERS["cache_write_5m"])

    # ---- end-to-end behavioral pin ---------------------------------------

    def test_end_to_end_matches_plexus_usage(self):
        old_journal = os.environ.get("EVENT_JOURNAL_DIR")
        old_ingest = os.environ.get("HEART_INGEST")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["EVENT_JOURNAL_DIR"] = str(root / "journal")
            os.environ["HEART_INGEST"] = "off"
            try:
                commit = make_repo(root)
                repo = str(root / "toyrepo")

                # plan.py / run.py: detect_verifiers(repo) on the goal repo
                verifiers = detect_verifiers(repo)
                self.assertTrue(any(v.name == "pytest" for v in verifiers))

                # run.py's _run_acceptance: Workspace(str(repo), base_commit),
                # ws.apply(diff), ws.destroy() -- a clean checkout, apply a
                # patch, then tear down. Exercise the same call pattern.
                ws = Workspace(repo, commit)
                try:
                    self.assertTrue(Path(ws.path).exists())
                finally:
                    ws.destroy()

                # run.py's TaskSpec construction (feature loop), same fields:
                # task_id, repo_path, base_commit, prompt, public_verifiers,
                # timeout_seconds. detect_verifiers's Verifier objects (which
                # look for tests/ or test_*.py) won't catch this single-file
                # toy repo's unittest module, so pin an explicit verifier the
                # way heart's own toy fixture does.
                from heart.taskspec import Verifier

                task = TaskSpec(
                    task_id="plexus-pin-toy",
                    repo_path=repo,
                    base_commit=commit,
                    prompt=FIX_CMD,  # shell agent executes the prompt as bash
                    public_verifiers=[
                        Verifier(name="unit",
                                 command="python3 -m unittest -q test_calc")
                    ],
                    timeout_seconds=60,
                )

                # run.py: best_episode(run_candidates(task, candidates,
                #                       agent=spec.agent, agent_cmd=spec.agent_cmd,
                #                       runs_dir=str(root / runs_dir)))
                ep = best_episode(run_candidates(
                    task, 1, agent="shell", runs_dir=str(root / "runs")))
                self.assertEqual(ep["outcome"], "pass")
            finally:
                if old_journal is None:
                    os.environ.pop("EVENT_JOURNAL_DIR", None)
                else:
                    os.environ["EVENT_JOURNAL_DIR"] = old_journal
                if old_ingest is None:
                    os.environ.pop("HEART_INGEST", None)
                else:
                    os.environ["HEART_INGEST"] = old_ingest


if __name__ == "__main__":
    unittest.main()
